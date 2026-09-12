#pragma once
#include <string>
#include <vector>
#include <optional>
#include <filesystem>

namespace fragile {

struct Task {
    std::string id;
    std::string title;
    std::string status;
    std::string due;
    std::string priority;
    std::filesystem::path source;
    int line = 0;
};

std::vector<Task> parse_tasks(const std::string& markdown, const std::filesystem::path& source);
std::vector<Task> scan_tasks(const std::filesystem::path& vault_root);
std::string task_to_markdown(const Task& t);
bool update_task_status(std::filesystem::path file, size_t line, const std::string& new_status);

}
