#pragma once
#include <string>
#include <vector>
#include <filesystem>

namespace fragile {
int fts_init(const std::filesystem::path& db);
int fts_index(const std::filesystem::path& db, const std::filesystem::path& file, const std::string& content);
std::vector<std::string> fts_search(const std::filesystem::path& db, const std::string& q);
int fts_rebuild(const std::filesystem::path& db, const std::filesystem::path& vault_root);
}
