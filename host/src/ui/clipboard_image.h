#pragma once
#include <string>
namespace ui {
// True when `path` names a raster image by extension (case-insensitive):
// .png .jpg .jpeg .bmp .gif .webp .tga
bool isImageAttachment(const std::string& path);
// "image/png" etc. for an image path; empty string for anything else.
std::string imageMimeForPath(const std::string& path);
// Absolute path for the Nth image pasted this session, under
// appdataDir()/Chudware/cache/pasted/<YYYY-MM-DD>/. Creates the directory.
// Deterministic given (seq, dateStamp); never returns a path outside that dir.
std::string pastedImagePath(int seq, const std::string& dateStamp);
// Cheap probe: does the system clipboard currently hold an image?
bool clipboardHasImage();
// Write the clipboard image to a new PNG under the pasted-images dir.
// Returns true and sets outPath; on failure returns false and sets err to a
// short, user-showable sentence.
bool clipboardImageToPng(std::string& outPath, std::string& err);
}
