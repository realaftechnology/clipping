// Google Drive (File Provider) helper for the offload step.
//   cloud status <path>  -> "uploaded" | "pending"     (has the file reached the cloud?)
//   cloud evict <path>   -> frees the local copy; the file stays in Drive, downloaded again only if opened
// Build: swiftc -O bin/cloud.swift -o bin/cloud
import Foundation
let a = CommandLine.arguments
guard a.count == 3 else { print("usage: cloud status|evict <path>"); exit(2) }
let url = URL(fileURLWithPath: a[2])
let v = try? url.resourceValues(forKeys: [.ubiquitousItemIsUploadedKey, .isUbiquitousItemKey])
if a[1] == "status" {
    print(v?.isUbiquitousItem == true && v?.ubiquitousItemIsUploaded == true ? "uploaded" : "pending")
} else if a[1] == "evict" {
    guard v?.ubiquitousItemIsUploaded == true else { print("not uploaded yet"); exit(1) }
    do { try FileManager.default.evictUbiquitousItem(at: url); print("evicted") } catch { print("evict failed: \(error)"); exit(1) }
}
