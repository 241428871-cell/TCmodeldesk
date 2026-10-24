import AppKit

// TCmodeldesk app icon: a decision node (white) handing work to two execution nodes whose
// colours echo the workbench status language (green = ready, amber = waiting).
// Geometry mirrors desktop/assets/tcmodeldesk-logo.svg and the sidebar logo in the web panel.
// Usage: make-icon <iconset-folder> [preview.png]

func drawIcon(in context: CGContext, pixels: CGFloat) {
    let unit = pixels / 1024
    // macOS icon grid: artwork occupies 824 of 1024 points.
    let body = CGRect(x: 100 * unit, y: 100 * unit, width: 824 * unit, height: 824 * unit)
    let path = CGPath(roundedRect: body, cornerWidth: 186 * unit, cornerHeight: 186 * unit, transform: nil)
    context.saveGState()
    context.setShadow(offset: CGSize(width: 0, height: -10 * unit), blur: 22 * unit,
        color: CGColor(gray: 0, alpha: 0.35))
    context.addPath(path)
    context.setFillColor(CGColor(red: 0.07, green: 0.09, blue: 0.16, alpha: 1))
    context.fillPath()
    context.restoreGState()

    context.saveGState()
    context.addPath(path)
    context.clip()
    let space = CGColorSpaceCreateDeviceRGB()
    let colors = [CGColor(red: 0.20, green: 0.27, blue: 0.47, alpha: 1), CGColor(red: 0.06, green: 0.08, blue: 0.15, alpha: 1)] as CFArray
    if let gradient = CGGradient(colorsSpace: space, colors: colors, locations: [0, 1]) {
        context.drawLinearGradient(gradient, start: CGPoint(x: 330 * unit, y: 924 * unit),
            end: CGPoint(x: 700 * unit, y: 100 * unit), options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
    }
    // Soft top highlight.
    let glow = [CGColor(gray: 1, alpha: 0.16), CGColor(gray: 1, alpha: 0)] as CFArray
    if let gradient = CGGradient(colorsSpace: space, colors: glow, locations: [0, 1]) {
        context.drawLinearGradient(gradient, start: CGPoint(x: 512 * unit, y: 924 * unit),
            end: CGPoint(x: 512 * unit, y: 560 * unit), options: [])
    }
    context.restoreGState()

    // Graph geometry in a 64 x 64 design grid mapped onto the artwork (y flipped for CoreGraphics).
    func point(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
        CGPoint(x: (100 + x / 64 * 824) * unit, y: (100 + (64 - y) / 64 * 824) * unit)
    }
    let radius = { (value: CGFloat) -> CGFloat in value / 64 * 824 * unit }
    let top = point(32, 18), left = point(17, 45), right = point(47, 45)
    context.setLineCap(.round)
    context.setStrokeColor(CGColor(red: 0.55, green: 0.67, blue: 1, alpha: 0.85))
    context.setLineWidth(radius(2.8))
    context.move(to: top); context.addLine(to: left)
    context.move(to: top); context.addLine(to: right)
    context.strokePath()
    context.setStrokeColor(CGColor(red: 0.55, green: 0.67, blue: 1, alpha: 0.35))
    context.setLineWidth(radius(2.2))
    context.move(to: left); context.addLine(to: right)
    context.strokePath()

    func node(_ center: CGPoint, _ size: CGFloat, _ color: CGColor) {
        context.setFillColor(CGColor(gray: 0, alpha: 0.16))
        context.fillEllipse(in: CGRect(x: center.x - radius(size + 1.2), y: center.y - radius(size + 1.2) - radius(0.8),
            width: radius(size + 1.2) * 2, height: radius(size + 1.2) * 2))
        context.setFillColor(color)
        context.fillEllipse(in: CGRect(x: center.x - radius(size), y: center.y - radius(size),
            width: radius(size) * 2, height: radius(size) * 2))
    }
    node(top, 8.5, CGColor(gray: 1, alpha: 1))
    node(left, 7, CGColor(red: 0.37, green: 0.82, blue: 0.69, alpha: 1))
    node(right, 7, CGColor(red: 1, green: 0.71, blue: 0.33, alpha: 1))
}

func renderPNG(pixels: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
        samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = NSSize(width: pixels, height: pixels)
    NSGraphicsContext.saveGraphicsState()
    let graphics = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.current = graphics
    drawIcon(in: graphics.cgContext, pixels: CGFloat(pixels))
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

let folder = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let suffix = scale == 2 ? "@2x" : ""
        try renderPNG(pixels: size * scale).write(to: folder.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
    }
}
if CommandLine.arguments.count > 2 {
    try renderPNG(pixels: 1024).write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
}
