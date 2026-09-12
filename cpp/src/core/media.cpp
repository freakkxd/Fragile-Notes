#include <filesystem>
#include <string>

namespace fragile {

std::string media_type(const std::filesystem::path& p){
    auto e=p.extension().string();
    if(e==".mp4"||e==".mkv"||e==".webm") return "video";
    if(e==".mp3"||e==".wav"||e==".flac") return "audio";
    if(e==".jpg"||e==".png"||e==".svg") return "image";
    return "file";
}

}
