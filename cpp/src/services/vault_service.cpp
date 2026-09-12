#include "fragile/vault.hpp"
#include <chrono>
#include <mutex>
#include <unordered_map>

namespace fragile {

static std::mutex m;
static std::unordered_map<std::string, std::pair<std::chrono::steady_clock::time_point, FileTreeNode>> cache;

FileTreeNode cached_scan(const std::filesystem::path& root){
    std::lock_guard<std::mutex> lk(m);
    auto it=cache.find(root.string());
    auto now=std::chrono::steady_clock::now();
    if(it!=cache.end() && std::chrono::duration_cast<std::chrono::seconds>(now-it->second.first).count()<10) return it->second.second;
    auto node=scan_vault(root);
    cache[root.string()]={now,node};
    return node;
}

}
