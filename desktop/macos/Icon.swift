import AppKit

let folder = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let image = NSImage(size: NSSize(width: pixels, height: pixels))
        image.lockFocus()
        NSColor(calibratedWhite: 0.1, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: pixels, height: pixels),
            xRadius: CGFloat(pixels) * 0.22, yRadius: CGFloat(pixels) * 0.22).fill()
        let style = NSMutableParagraphStyle()
        style.alignment = .center
        let font = NSFont.systemFont(ofSize: CGFloat(pixels) * 0.4, weight: .semibold)
        let text = NSString(string: "TC")
        let attributes: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.white, .paragraphStyle: style]
        text.draw(in: NSRect(x: 0, y: CGFloat(pixels) * 0.24, width: CGFloat(pixels), height: CGFloat(pixels) * 0.55), withAttributes: attributes)
        image.unlockFocus()
        let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
        let data = bitmap.representation(using: .png, properties: [:])!
        let suffix = scale == 2 ? "@2x" : ""
        try data.write(to: folder.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
    }
}
