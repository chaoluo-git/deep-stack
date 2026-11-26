import CoreGraphics
import CoreML
import CoreText
import Foundation
import ImageIO
import Vision

private struct Region: Decodable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let kind: String

    func rectangle(in viewport: CGRect) -> CGRect {
        CGRect(
            x: viewport.minX + viewport.width * x,
            y: viewport.minY + viewport.height * y,
            width: viewport.width * width,
            height: viewport.height * height
        ).integral.intersection(viewport)
    }
}

private struct Input: Decodable {
    let path: String
    let viewport: [Int]?
    let regions: [String: Region]
}

private struct Observation: Encodable {
    let text: String
    let confidence: Float
    let centerX: Double
    let centerY: Double

    enum CodingKeys: String, CodingKey {
        case text, confidence
        case centerX = "center_x"
        case centerY = "center_y"
    }
}

private struct CornerCard: Encodable {
    let suit: String
    let overlap: Double
    let rankObservations: [Observation]

    enum CodingKeys: String, CodingKey {
        case suit, overlap
        case rankObservations = "rank_observations"
    }
}

private struct Result: Encodable {
    let observations: [Observation]
    let emptyFelt: Bool
    let cardSurface: Bool
    let cornerCard: CornerCard?

    enum CodingKeys: String, CodingKey {
        case observations
        case emptyFelt = "empty_felt"
        case cardSurface = "card_surface"
        case cornerCard = "corner_card"
    }
}

private enum ScreenshotError: LocalizedError {
    case invalidImage
    case invalidViewport

    var errorDescription: String? {
        switch self {
        case .invalidImage:
            return "不能解码截图。"
        case .invalidViewport:
            return "截图裁切区域超出图片边界。"
        }
    }
}

private func fullResolutionImage(at path: String) throws -> CGImage {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as NSDictionary?,
          let width = properties[kCGImagePropertyPixelWidth] as? Int,
          let height = properties[kCGImagePropertyPixelHeight] as? Int,
          width > 0, height > 0 else {
        throw ScreenshotError.invalidImage
    }
    let options = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: max(width, height)
    ] as CFDictionary
    guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options) else {
        throw ScreenshotError.invalidImage
    }
    return image
}

private let rgb = CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB()

private func enlarged(_ image: CGImage) -> CGImage {
    // Retina screenshots already contain enough pixels. Upscale smaller crops
    // so the thinnest strokes remain visible to Vision's text detector.
    guard image.height < 85 else { return image }
    let scale = min(4, max(2, 120 / image.height))
    guard let context = CGContext(
        data: nil, width: image.width * scale, height: image.height * scale,
        bitsPerComponent: 8, bytesPerRow: 0, space: rgb,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        return image
    }
    context.interpolationQuality = .high
    context.draw(image, in: CGRect(x: 0, y: 0, width: context.width, height: context.height))
    return context.makeImage() ?? image
}

private enum Surface {
    case felt, card, unknown
}

private func surface(of image: CGImage) -> Surface {
    let size = 16
    let bytesPerRow = 4 * size
    guard let context = CGContext(
        data: nil, width: size, height: size, bitsPerComponent: 8,
        bytesPerRow: bytesPerRow, space: rgb,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue
    ) else {
        return .unknown
    }
    context.interpolationQuality = .medium
    context.draw(image, in: CGRect(x: 0, y: 0, width: size, height: size))
    guard let pixels = context.data?.assumingMemoryBound(to: UInt8.self) else { return .unknown }

    var greenPixels = 0
    var cardPixels = 0
    for offset in stride(from: 0, to: size * bytesPerRow, by: 4) {
        let red = Int(pixels[offset])
        let green = Int(pixels[offset + 1])
        let blue = Int(pixels[offset + 2])
        guard pixels[offset + 3] > 240 else { continue }
        if green >= red + 10 && green >= blue + 6 {
            greenPixels += 1
        }
        let lightest = max(red, green, blue)
        let darkest = min(red, green, blue)
        if darkest >= 96 && lightest - darkest <= 25 {
            cardPixels += 1
        }
    }
    let area = size * size
    if greenPixels * 100 >= area * 97 { return .felt }
    if cardPixels * 100 >= area * 83 { return .card }
    return .unknown
}


private let maskSize = 24

private func normalizedMask(_ pixels: [Bool], width: Int, height: Int) -> [Bool]? {
    let ink = pixels.indices.filter { pixels[$0] }
    guard ink.count >= 12,
          let top = ink.first.map({ $0 / width }),
          let bottom = ink.last.map({ $0 / width }),
          let left = ink.map({ $0 % width }).min(),
          let right = ink.map({ $0 % width }).max(),
          left > 0, right < width - 1, top > 0, bottom < height - 1,
          right - left >= 4, bottom - top >= 4 else {
        return nil
    }
    let boxWidth = right - left + 1
    let boxHeight = bottom - top + 1
    guard Double(boxWidth) / Double(boxHeight) >= 0.65,
          Double(boxWidth) / Double(boxHeight) <= 1.65,
          ink.count * 100 >= boxWidth * boxHeight * 30,
          ink.count * 100 <= boxWidth * boxHeight * 89 else {
        return nil
    }
    return (0 ..< maskSize * maskSize).map { index in
        let x = left + ((index % maskSize) * 2 + 1) * boxWidth / (2 * maskSize)
        let y = top + ((index / maskSize) * 2 + 1) * boxHeight / (2 * maskSize)
        return pixels[y * width + x]
    }
}

private func systemSuitMask(_ symbol: String) -> [Bool]? {
    let font = CTFontCreateWithName("Apple Symbols" as CFString, 48, nil)
    guard var character = symbol.utf16.first else { return nil }
    var glyph = CGGlyph(0)
    let size = 64
    guard CTFontGetGlyphsForCharacters(font, &character, &glyph, 1),
          let path = CTFontCreatePathForGlyph(font, glyph, nil),
          let context = CGContext(
              data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: size,
              space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue
          ),
          let data = context.data?.assumingMemoryBound(to: UInt8.self) else {
        return nil
    }
    let bounds = path.boundingBoxOfPath
    guard !bounds.isEmpty else { return nil }
    let scale = 54 / max(bounds.width, bounds.height)
    context.translateBy(x: 5, y: 5)
    context.scaleBy(x: scale, y: scale)
    context.translateBy(x: -bounds.minX, y: -bounds.minY)
    context.addPath(path)
    context.setFillColor(gray: 1, alpha: 1)
    context.fillPath()
    let pixels = (0 ..< size * size).map { data[$0] >= 128 }
    return normalizedMask(pixels, width: size, height: size)
}

private let suitMasks = ["s": "♠", "h": "♥", "d": "♦", "c": "♣"].compactMapValues(systemSuitMask)

private func matchSuit(in image: CGImage) -> (suit: String, overlap: Double)? {
    guard let context = CGContext(
        data: nil, width: image.width, height: image.height, bitsPerComponent: 8,
        bytesPerRow: 4 * image.width, space: rgb,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue
    ),
    let data = context.data?.assumingMemoryBound(to: UInt8.self) else {
        return nil
    }
    context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
    var redInk: [Bool] = []
    var blackInk: [Bool] = []
    for offset in stride(from: 0, to: context.bytesPerRow * image.height, by: 4) {
        let red = Int(data[offset])
        let green = Int(data[offset + 1])
        let blue = Int(data[offset + 2])
        redInk.append(red - green >= 48 && red - blue >= 32)
        blackInk.append(max(red, green, blue) <= 105)
    }

    let redCount = redInk.filter { $0 }.count
    let blackCount = blackInk.filter { $0 }.count
    guard max(redCount, blackCount) >= 12, min(redCount, blackCount) * 4 <= max(redCount, blackCount) else {
        return nil
    }
    let red = redCount > blackCount
    guard let mask = normalizedMask(red ? redInk : blackInk, width: image.width, height: image.height) else {
        return nil
    }

    let scores: [(suit: String, overlap: Double)] = (red ? ["h", "d"] : ["s", "c"]).compactMap { suit in
        guard let reference = suitMasks[suit] else { return nil }
        let foreground = mask.filter { $0 }.count + reference.filter { $0 }.count
        let intersection = zip(mask, reference).filter { $0 && $1 }.count
        return (suit, Double(2 * intersection) / Double(foreground))
    }.sorted { $0.overlap > $1.overlap }
    let minimumOverlap = red ? 0.86 : 0.90
    let margin = red ? 0.11 : 0.07
    guard scores.count == 2, scores[0].overlap >= minimumOverlap,
          scores[0].overlap - scores[1].overlap >= margin else {
        return nil
    }
    return scores[0]
}

private func cardCorner(in image: CGImage, observations: [Observation]) throws -> CornerCard? {
    let width = Double(image.width)
    let height = Double(image.height)
    let suitRect = CGRect(x: width * 0.03, y: height * 0.32, width: width * 0.39, height: height * 0.25).integral
    let rankRect = CGRect(x: width * 0.015, y: height * 0.015, width: width * 0.56, height: height * 0.31).integral
    guard let suitImage = image.cropping(to: suitRect),
          let candidate = matchSuit(in: suitImage),
          let rankImage = image.cropping(to: rankRect) else {
        return nil
    }
    let wholeCardRanks = observations.filter { $0.centerX < 0.56 && $0.centerY < 0.32 }
    var ranks = try readText(in: rankImage, kind: "card") + wholeCardRanks
    if ranks.isEmpty, let repeated = repeatedRankImage(rankImage) {
        ranks = try readText(in: repeated, kind: "card")
    }
    return CornerCard(suit: candidate.suit, overlap: candidate.overlap, rankObservations: ranks)
}

private func repeatedRankImage(_ image: CGImage) -> CGImage? {
    // Some Vision revisions ignore isolated letters. Repeating the same crop gives
    // the text detector a line of glyphs without introducing a new candidate rank.
    guard let context = CGContext(
        data: nil, width: image.width * 3, height: image.height, bitsPerComponent: 8,
        bytesPerRow: 0, space: rgb, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        return nil
    }
    for index in 0 ..< 3 {
        let rectangle = CGRect(x: index * image.width, y: 0, width: image.width, height: image.height)
        context.draw(image, in: rectangle)
    }
    return context.makeImage()
}

private func readText(in image: CGImage, kind: String) throws -> [Observation] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = kind == "label" ? ["zh-Hans", "en-US"] : ["en-US"]
    request.usesLanguageCorrection = false
    request.minimumTextHeight = 0.09
    // CPU execution avoids neural-engine model compilation during a CLI scan.
    if #available(macOS 14.0, *) {
        for (stage, devices) in try request.supportedComputeStageDevices {
            if let cpu = devices.first(where: { if case .cpu = $0 { true } else { false } }) {
                request.setComputeDevice(cpu, for: stage)
            }
        }
    } else {
        request.usesCPUOnly = true
    }

    try VNImageRequestHandler(cgImage: enlarged(image), options: [:]).perform([request])
    return (request.results ?? []).compactMap { observation -> Observation? in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        return Observation(
            text: candidate.string, confidence: candidate.confidence,
            centerX: observation.boundingBox.midX, centerY: 1 - observation.boundingBox.midY
        )
    }

}

private func recognize(_ image: CGImage, kind: String) throws -> Result {
    let surface = kind == "card" ? surface(of: image) : .unknown
    if surface == .felt {
        return Result(observations: [], emptyFelt: true, cardSurface: false, cornerCard: nil)
    }
    let observations = try readText(in: image, kind: kind)
    let corner = surface == .card ? try cardCorner(in: image, observations: observations) : nil
    return Result(observations: observations, emptyFelt: false, cardSurface: surface == .card, cornerCard: corner)
}

private func process(_ input: Input) throws -> [String: Result] {
    let image = try fullResolutionImage(at: input.path)
    let bounds = CGRect(x: 0, y: 0, width: image.width, height: image.height)
    let viewport: CGRect
    if let crop = input.viewport, crop.count == 4 {
        viewport = CGRect(x: crop[0], y: crop[1], width: crop[2], height: crop[3])
        guard !viewport.isEmpty && bounds.contains(viewport) else {
            throw ScreenshotError.invalidViewport
        }
    } else {
        viewport = bounds
    }

    var results = [String: Result]()
    for (name, region) in input.regions.sorted(by: { $0.key < $1.key }) {
        let rectangle = region.rectangle(in: viewport)
        guard !rectangle.isEmpty, let regionImage = image.cropping(to: rectangle) else { continue }
        results[name] = try recognize(regionImage, kind: region.kind)
    }
    return results
}

do {
    let input = try JSONDecoder().decode(Input.self, from: FileHandle.standardInput.readDataToEndOfFile())
    let data = try JSONEncoder().encode(process(input))
    FileHandle.standardOutput.write(data)
} catch {
    FileHandle.standardError.write(Data((error.localizedDescription + "\n").utf8))
    exit(1)
}
