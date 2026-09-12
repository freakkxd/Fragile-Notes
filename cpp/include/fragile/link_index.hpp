#pragma once
#include <string>
#include <vector>
#include <unordered_map>
#include <filesystem>

namespace fragile {
struct Link { std::string from; std::string to; int line = 0; };
struct BacklinkIndex {
  std::unordered_map<std::string, std::vector<Link>> forward;
  std::unordered_map<std::string, std::vector<Link>> back;
};
std::vector<Link> extract_links(const std::string& md, const std::string& from_path);
BacklinkIndex build_link_index(const std::filesystem::path& vault_root);
std::vector<std::string> extract_tags(const std::string& md);
}
