// Face boxes for every .jpg in a folder, via macOS Vision. Output: JSON [{file, faces:[{x,y,w,h,c}]}], coords 0-1, origin top-left.
import Foundation
import Vision
let dir = URL(fileURLWithPath: CommandLine.arguments[1])
let files = (try FileManager.default.contentsOfDirectory(atPath: dir.path)).filter { $0.hasSuffix(".jpg") }.sorted()
var out: [[String: Any]] = []
for f in files {
    let req = VNDetectFaceRectanglesRequest()
    try? VNImageRequestHandler(url: dir.appendingPathComponent(f), options: [:]).perform([req])
    let faces = (req.results ?? []).map { o -> [String: Double] in
        let b = o.boundingBox
        return ["x": b.origin.x, "y": 1 - b.origin.y - b.height, "w": b.width, "h": b.height, "c": Double(o.confidence)]
    }
    out.append(["file": f, "faces": faces])
}
FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: out))
