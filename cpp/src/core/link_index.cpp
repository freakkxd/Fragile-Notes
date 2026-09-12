#include "fragile/link_index.hpp"
#include <regex>
#include <fstream>
#include <sstream>

namespace fragile {

std::vector<Link> extract_links(const std::string& md, const std::string& from_path) {
  std::vector<Link> out;
  static std::regex re(R"(\[\[([^\]|#]+)(?:#[^\]]*)?(?:\|[^\]]*)?\]\])");
  std::istringstream iss(md);
  std::string line; int n=0;
  while (std::getline(iss, line)) {
    n++;
    std::sregex_iterator it(line.begin(), line.end(), re), end;
    for (; it!=end; ++it) {
      Link l; l.from = from_path; l.to = (*it)[1].str(); l.line = n;
      // trim
      l.to.erase(0, l.to.find_first_not_of(" \t"));
      l.to.erase(l.to.find_last_not_of(" \t")+1);
      out.push_back(std::move(l));
    }
  }
  return out;
}

BacklinkIndex build_link_index(const std::filesystem::path& vault_root) {
  BacklinkIndex idx;
  for (auto& p : std::filesystem::recursive_directory_iterator(vault_root)) {
    if (!p.is_regular_file() || p.path().extension() != ".md") continue;
    std::ifstream f(p.path());
    std::string txt((std::istreambuf_iterator<char>(f)), {});
    auto links = extract_links(txt, p.path().string());
    for (auto& l : links) {
      idx.forward[l.from].push_back(l);
      idx.back[l.to].push_back(l);
    }
  }
  return idx;
}

std::vector<std::string> extract_tags(const std::string& md) {
  std::vector<std::string> out;
  static std::regex re(R"((?:^|\s)#([A-Za-z0-9/_-]+))");
  std::sregex_iterator it(md.begin(), md.end(), re), end;
  for (; it!=end; ++it) out.push_back((*it)[1].str());
  return out;
}

}
