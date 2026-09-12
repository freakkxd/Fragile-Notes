#pragma once
#include <string>
#include <vector>
#include <filesystem>

namespace fragile {

struct Workspace {
    std::string path;
    std::string name;
};

std::vector<Workspace> get_workspaces(const std::string& settings_json);
void ensure_workspaces(std::string& settings_json);
Workspace switch_workspace(const std::string& target);

}
