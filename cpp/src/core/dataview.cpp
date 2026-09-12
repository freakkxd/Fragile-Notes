#include <string>
#include <vector>
#include <regex>
#include <sstream>

namespace fragile {

std::string expand_dataview(const std::string& md){
    std::string out=md;
    static std::regex re(R"(```dataview\s*\n([\s\S]*?)\n```)");
    out = std::regex_replace(out, re, "<div class=\"dataview\"><table><tr><td>dataview rendered</td></tr></table></div>");
    return out;
}

}
