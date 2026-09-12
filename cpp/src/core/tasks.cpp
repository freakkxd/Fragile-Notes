#include "fragile/tasks.hpp"
#include <regex>
#include <fstream>
#include <sstream>

namespace fragile {

std::vector<Task> parse_tasks(const std::string& md, const std::filesystem::path& src){
    std::vector<Task> out;
    static std::regex re(R"(- \[( |x|X|-)\]\s*(.*))");
    std::istringstream iss(md);
    std::string line; int n=0;
    while(std::getline(iss,line)){
        n++;
        std::smatch m;
        if(std::regex_search(line,m,re)){
            Task t; t.source=src; t.line=n; t.title=m[2].str();
            std::string mark=m[1].str();
            if(mark=="x"||mark=="X") t.status="done";
            else if(mark=="-") t.status="cancelled";
            else t.status="todo";
            t.id=src.string()+":"+std::to_string(n);
            auto pos=t.title.find("📅");
            if(pos!=std::string::npos) t.due=t.title.substr(pos+2);
            out.push_back(std::move(t));
        }
    }
    return out;
}

std::vector<Task> scan_tasks(const std::filesystem::path& root){
    std::vector<Task> all;
    for(auto &p: std::filesystem::recursive_directory_iterator(root)){
        if(!p.is_regular_file()) continue;
        if(p.path().extension()!=".md") continue;
        std::ifstream f(p.path());
        std::string txt((std::istreambuf_iterator<char>(f)), {});
        auto v=parse_tasks(txt, p.path());
        all.insert(all.end(), v.begin(), v.end());
    }
    return all;
}

std::string task_to_markdown(const Task& t){
    std::string mark = t.status=="done"?"x":" ";
    return "- ["+mark+"] "+t.title;
}

bool update_task_status(std::filesystem::path file, size_t line, const std::string& ns){
    std::ifstream f(file);
    if(!f) return false;
    std::vector<std::string> lines; std::string l;
    while(std::getline(f,l)) lines.push_back(l);
    if(line==0||line>lines.size()) return false;
    auto &t=lines[line-1];
    std::regex re(R"(- \[( |x|X|-)\])");
    std::string rep = ns=="done"?"- [x]": ns=="cancelled"?"- [-]":"- [ ]";
    t = std::regex_replace(t, re, rep);
    std::ofstream o(file); for(auto &x: lines) o<<x<<"\n";
    return true;
}

}
