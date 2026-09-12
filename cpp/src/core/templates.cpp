#include <string>
#include <filesystem>
#include <fstream>
#include <chrono>
#include <iomanip>

namespace fragile {

std::string apply_template(const std::string& tmpl, const std::string& title){
    std::string out=tmpl;
    auto now=std::chrono::system_clock::now();
    std::time_t t=std::chrono::system_clock::to_time_t(now);
    std::stringstream ss; ss<<std::put_time(std::localtime(&t), "%Y-%m-%d");
    std::string date=ss.str();
    size_t p;
    while((p=out.find("{{title}}"))!=std::string::npos) out.replace(p,9,title);
    while((p=out.find("{{date}}"))!=std::string::npos) out.replace(p,8,date);
    return out;
}

}
