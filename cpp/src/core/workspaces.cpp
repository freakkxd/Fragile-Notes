#include "fragile/workspaces.hpp"
#include <fstream>
#include <sstream>

namespace fragile {

std::vector<Workspace> get_workspaces(const std::string& j){
    std::vector<Workspace> out;
    size_t pos=j.find("\"vaults\"");
    if(pos==std::string::npos) pos=j.find("\"workspaces\"");
    if(pos==std::string::npos) return out;
    size_t a=j.find('[',pos), b=j.find(']',a);
    if(a==std::string::npos||b==std::string::npos) return out;
    std::string arr=j.substr(a,b-a);
    size_t p=0;
    while((p=arr.find("\"path\"",p))!=std::string::npos){
        size_t q=arr.find('"',p+6); size_t r=arr.find('"',q+1);
        std::string path=arr.substr(q+1,r-q-1);
        size_t n=arr.find("\"name\"",r); std::string name;
        if(n!=std::string::npos){ size_t q2=arr.find('"',n+6); size_t r2=arr.find('"',q2+1); name=arr.substr(q2+1,r2-q2-1); }
        else name=std::filesystem::path(path).filename().string();
        out.push_back({path,name}); p=r+1;
    }
    return out;
}

void ensure_workspaces(std::string&){}

Workspace switch_workspace(const std::string& t){
    return Workspace{t, std::filesystem::path(t).filename().string()};
}

}
