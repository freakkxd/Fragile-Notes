#include <string>
#include <vector>
#include <filesystem>
#include <sqlite3.h>

namespace fragile {

int fts_init(const std::filesystem::path& db){
    sqlite3* h; sqlite3_open(db.string().c_str(), &h);
    sqlite3_exec(h, "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(path, content);", nullptr,nullptr,nullptr);
    sqlite3_close(h); return 0;
}

int fts_index(const std::filesystem::path& db, const std::filesystem::path& file, const std::string& content){
    sqlite3* h; sqlite3_open(db.string().c_str(), &h);
    sqlite3_stmt* s; sqlite3_prepare_v2(h, "INSERT INTO fts(path,content) VALUES(?,?);", -1, &s, nullptr);
    sqlite3_bind_text(s,1,file.string().c_str(),-1,SQLITE_TRANSIENT);
    sqlite3_bind_text(s,2,content.c_str(),-1,SQLITE_TRANSIENT);
    sqlite3_step(s); sqlite3_finalize(s); sqlite3_close(h); return 0;
}

std::vector<std::string> fts_search(const std::filesystem::path& db, const std::string& q){
    std::vector<std::string> r;
    sqlite3* h; sqlite3_open(db.string().c_str(), &h);
    sqlite3_stmt* s; sqlite3_prepare_v2(h, "SELECT path FROM fts WHERE fts MATCH ?;", -1, &s, nullptr);
    sqlite3_bind_text(s,1,q.c_str(),-1,SQLITE_TRANSIENT);
    while(sqlite3_step(s)==SQLITE_ROW) r.push_back((const char*)sqlite3_column_text(s,0));
    sqlite3_finalize(s); sqlite3_close(h); return r;
}

}
