// pdfpages: count a PDF's pages, or render them to PNG for a local vision model.
//   pdfpages FILE.pdf                     prints the page count
//   pdfpages FILE.pdf OUTDIR MAXPAGES EDGE  writes OUTDIR/page-001.png ... (longer side EDGE px)
// Built on first use by elves/attachments.py with swiftc; macOS only (PDFKit, AppKit).
import AppKit
import PDFKit

let args = CommandLine.arguments
guard args.count == 2 || args.count == 5,
      let doc = PDFDocument(url: URL(fileURLWithPath: args[1])) else {
    FileHandle.standardError.write("usage: pdfpages FILE.pdf [OUTDIR MAXPAGES EDGE]\n".data(using: .utf8)!)
    exit(2)
}
print(doc.pageCount)
if args.count == 2 { exit(0) }

let out = URL(fileURLWithPath: args[2], isDirectory: true)
let maxPages = Int(args[3]) ?? 20
let edge = CGFloat(Double(args[4]) ?? 1600)
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
for i in 0..<min(doc.pageCount, maxPages) {
    guard let page = doc.page(at: i) else { continue }
    let box = page.bounds(for: .mediaBox)
    let scale = edge / max(box.width, box.height, 1)
    let size = NSSize(width: (box.width * scale).rounded(), height: (box.height * scale).rounded())
    let image = page.thumbnail(of: size, for: .mediaBox)
    guard let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else { exit(3) }
    try png.write(to: out.appendingPathComponent(String(format: "page-%03d.png", i + 1)))
}
