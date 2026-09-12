#pragma once
#include <string>
#include <vector>
#include <unordered_map>
#include <filesystem>
#include <optional>

namespace fragile {

struct FileTreeNode {
    std::string name;
    std::string path;
    std::vector<FileTreeNode> dirs;
    std::vector<std::pair<std::string,std::string>> files;
};

struct Frontmatter {
    std::unordered_map<std::string,std::string> fields;
    std::string body;
};

Frontmatter parse_frontmatter(const std::string& text);
std::string serialize_frontmatter(const std::unordered_map<std::string,std::string>& fm, const std::string& body);
FileTreeNode scan_vault(const std::filesystem::path& root);
bool is_text_file(const std::filesystem::path& p);
std::vector<std::filesystem::path> list_notes(const std::filesystem::path& root);
std::string read_note(const std::filesystem::path& p);
void write_note(const std::filesystem::path& p, const std::string& content);
std::string note_title(const std::filesystem::path& p);

}
