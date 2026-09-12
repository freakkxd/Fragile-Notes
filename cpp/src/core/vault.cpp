#include "fragile/vault.hpp"
#include <fstream>
#include <sstream>
#include <regex>
#include <algorithm>

namespace fragile {

bool is_text_file(const std::filesystem::path& p) {
    static const std::unordered_map<std::string,int> allowed = {
        {".md",1},{".txt",1},{".html",1},{".htm",1},{".css",1},{".js",1},{".ts",1},{".jsx",1},{".tsx",1},{".json",1},{".yaml",1},{".yml",1},{".toml",1},{".ini",1},{".cfg",1},{".py",1},{".cpp",1},{".h",1},{".hpp",1},{".c",1},{".rs",1},{".go",1},{".java",1},{".sh",1},{".xml",1},{".svg",1},{".csv",1},{".log",1},{".enc",1},{".pdf",1},{".mdx",1}
    };
    auto ext = p.extension().string();
    std::transform(ext.begin(), ext.end(), ext.begin(), ::tolower);
    if (allowed.count(ext)) return true;
    std::ifstream f(p, std::ios::binary);
    if (!f) return false;
    char buf[1024]; f.read(buf, sizeof(buf));
    auto n = f.gcount();
    if (n==0) return true;
    for (int i=0;i<n;i++) if (buf[i]=='\0') return false;
    return true;
}

Frontmatter parse_frontmatter(const std::string& text) {
    Frontmatter r;
    static std::regex re(R"(^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?)", std::regex::multiline);
    std::smatch m;
    if (!std::regex_search(text, m, re)) { r.body=text; return r; }
    std::string yaml = m[1].str();
    std::istringstream iss(yaml);
    std::string line;
    while (std::getline(iss, line)) {
        auto pos = line.find(':');
        if (pos==std::string::npos) continue;
        std::string k=line.substr(0,pos), v=line.substr(pos+1);
        auto trim=[](std::string s){ s.erase(0,s.find_first_not_of(" \t\"'")); s.erase(s.find_last_not_of(" \t\"'")+1); return s; };
        r.fields[trim(k)] = trim(v);
    }
    r.body = text.substr(m[0].length());
    return r;
}

std::string serialize_frontmatter(const std::unordered_map<std::string,std::string>& fm, const std::string& body) {
    if (fm.empty()) return body;
    std::string out="---\n";
    for (auto &kv: fm) out += kv.first + ": " + kv.second + "\n";
    out += "---\n" + body;
    return out;
}

FileTreeNode scan_vault(const std::filesystem::path& root) {
    FileTreeNode node; node.name=root.filename().string(); node.path=root.string();
    if (!std::filesystem::exists(root)) return node;
    for (auto &e: std::filesystem::directory_iterator(root)) {
        auto name=e.path().filename().string();
        if (name.rfind(".",0)==0) continue;
        if (name=="node_modules"||name==".git"||name=="dist"||name=="build"||name=="target"||name==".venv") continue;
        if (e.is_directory()) {
            node.dirs.push_back(scan_vault(e.path()));
        } else if (e.is_regular_file() && is_text_file(e.path())) {
            node.files.emplace_back(name, e.path().string());
        }
    }
    std::sort(node.dirs.begin(), node.dirs.end(), [](auto& a, auto& b){return a.name<b.name;});
    std::sort(node.files.begin(), node.files.end(), [](auto& a, auto& b){return a.first<b.first;});
    return node;
}

std::vector<std::filesystem::path> list_notes(const std::filesystem::path& root) {
    std::vector<std::filesystem::path> out;
    auto tree=scan_vault(root);
    std::function<void(const FileTreeNode&)> walk=[&](const FileTreeNode& n){
        for (auto &f: n.files) out.push_back(f.second);
        for (auto &d: n.dirs) walk(d);
    };
    walk(tree);
    return out;
}

std::string read_note(const std::filesystem::path& p) {
    std::ifstream f(p); if(!f) return "";
    return std::string(std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>());
}

void write_note(const std::filesystem::path& p, const std::string& content) {
    std::filesystem::create_directories(p.parent_path());
    std::ofstream f(p); f<<content;
}

std::string note_title(const std::filesystem::path& p) {
    auto txt=read_note(p);
    auto fm=parse_frontmatter(txt);
    auto it=fm.fields.find("title");
    if (it!=fm.fields.end() && !it->second.empty()) return it->second;
    std::istringstream iss(fm.body);
    std::string line;
    while(std::getline(iss,line)){
        if(line.rfind("# ",0)==0) return line.substr(2);
    }
    return p.stem().string();
}

}
