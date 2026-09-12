#pragma once
#include <string>
#include <vector>
#include <unordered_map>
#include <cstdint>

namespace fragile {

struct CharId {
    uint64_t counter;
    std::string replica;
    bool operator==(const CharId& o) const { return counter==o.counter && replica==o.replica; }
};

struct CharItem {
    CharId id;
    std::string ch;
    bool deleted = false;
    int64_t timestamp = 0;
    double key = 0;
};

class RGAText {
public:
    explicit RGAText(std::string doc_id, std::string replica_id);
    std::string to_text() const;
    void set_text(const std::string& text);
    void local_insert(size_t offset, const std::string& text);
    void local_delete(size_t offset, size_t len);
    bool merge(const RGAText& other);
    std::string to_json() const;
    static RGAText from_json(const std::string& s);
    const std::string& doc_id() const { return doc_id_; }
private:
    std::string doc_id_;
    std::string replica_id_;
    uint64_t counter_ = 0;
    std::vector<CharItem> items_;
    std::unordered_map<std::string,int64_t> vector_;
    CharId next_id();
    void maybe_rebalance();
};

struct LWWRegister {
    std::string value;
    int64_t timestamp = 0;
    std::string replica_id;
    void set(const std::string& v, int64_t ts, const std::string& rid);
    bool merge(const LWWRegister& o);
};

class LWWDocument {
public:
    explicit LWWDocument(std::string doc_id, std::string replica_id);
    std::string text() const { return reg_.value; }
    void set_text(const std::string& t);
    bool merge(const LWWDocument& o);
    std::string to_json() const;
    static LWWDocument from_json(const std::string& s);
private:
    std::string doc_id_;
    std::string replica_id_;
    LWWRegister reg_;
    std::unordered_map<std::string,int64_t> vector_;
};

}
