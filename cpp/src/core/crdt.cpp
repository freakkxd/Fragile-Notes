#include "fragile/crdt.hpp"
#include <algorithm>
#include <chrono>
#include <sstream>

namespace fragile {

static int64_t now_ms(){ return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count(); }

void LWWRegister::set(const std::string& v, int64_t ts, const std::string& rid){
    if (std::make_pair(ts,rid) >= std::make_pair(timestamp,replica_id)){ value=v; timestamp=ts; replica_id=rid; }
}
bool LWWRegister::merge(const LWWRegister& o){
    if (std::make_pair(o.timestamp,o.replica_id) > std::make_pair(timestamp,replica_id)){ value=o.value; timestamp=o.timestamp; replica_id=o.replica_id; return true; }
    return false;
}
LWWDocument::LWWDocument(std::string id, std::string rid): doc_id_(std::move(id)), replica_id_(std::move(rid)){ reg_.replica_id=replica_id_; vector_[replica_id_]=0; }
void LWWDocument::set_text(const std::string& t){ int64_t ts=now_ms(); reg_.set(t,ts,replica_id_); vector_[replica_id_]=std::max(vector_[replica_id_],ts); }
bool LWWDocument::merge(const LWWDocument& o){ bool c=reg_.merge(o.reg_); for(auto &kv:o.vector_) vector_[kv.first]=std::max(vector_[kv.first],kv.second); vector_[replica_id_]=std::max(vector_[replica_id_],reg_.timestamp); return c; }
std::string LWWDocument::to_json() const { std::ostringstream s; s<<"{\"type\":\"lww\",\"doc_id\":\""<<doc_id_<<"\",\"value\":\""<<reg_.value<<"\"}"; return s.str(); }
LWWDocument LWWDocument::from_json(const std::string&){ return LWWDocument("",""); }

RGAText::RGAText(std::string id, std::string rid): doc_id_(std::move(id)), replica_id_(std::move(rid)){ vector_[replica_id_]=0; }
CharId RGAText::next_id(){ return CharId{++counter_, replica_id_}; }
std::string RGAText::to_text() const {
    std::vector<CharItem> sorted=items_;
    std::sort(sorted.begin(), sorted.end(), [](auto& a, auto& b){ if(a.key!=b.key) return a.key<b.key; if(a.id.replica!=b.id.replica) return a.id.replica<b.id.replica; return a.id.counter<b.id.counter; });
    std::string out; for(auto &it: sorted) if(!it.deleted && !it.ch.empty()) out+=it.ch; return out;
}
void RGAText::set_text(const std::string& text){
    int64_t ts=now_ms();
    for(auto &it: items_) if(!it.deleted && !it.ch.empty()){ it.deleted=true; it.timestamp=std::max(it.timestamp,ts); }
    std::vector<CharItem> keep; for(auto &it: items_) if(it.deleted) keep.push_back(it);
    items_=keep;
    double base=0; for(auto &it: items_) base=std::max(base,it.key);
    for(size_t i=0;i<text.size();i++){ auto id=next_id(); items_.push_back(CharItem{id,std::string(1,text[i]),false,ts,base+1+i}); }
    vector_[replica_id_]=std::max(vector_[replica_id_],ts);
}
void RGAText::local_insert(size_t offset, const std::string& text){
    if(text.empty()) return;
    auto visible=[&](){ std::vector<CharItem> s=items_; std::sort(s.begin(),s.end(),[](auto& a, auto& b){return a.key<b.key;}); std::vector<CharItem> v; for(auto &it:s) if(!it.deleted && !it.ch.empty()) v.push_back(it); return v; }();
    offset=std::min(offset, visible.size());
    double left=0,right=1;
    if(visible.empty()){ double base=0; for(auto &it:items_) base=std::max(base,it.key); left=base; right=base+text.size()+1; }
    else if(offset==0) { left=visible[0].key-1; right=visible[0].key; }
    else if(offset>=visible.size()){ left=visible.back().key; double mx=left; for(auto &it:items_) mx=std::max(mx,it.key); right=mx+1; }
    else { left=visible[offset-1].key; right=visible[offset].key; }
    int64_t ts=now_ms();
    for(size_t i=0;i<text.size();i++){ double key = left + (right-left)*(double)(i+1)/(text.size()+1); auto id=next_id(); items_.push_back(CharItem{id,std::string(1,text[i]),false,ts,key}); }
    vector_[replica_id_]=std::max(vector_[replica_id_],ts);
}
void RGAText::local_delete(size_t offset, size_t len){
    auto visible=[&](){ std::vector<CharItem> s=items_; std::sort(s.begin(),s.end(),[](auto& a, auto& b){return a.key<b.key;}); std::vector<CharItem> v; for(auto &it:s) if(!it.deleted && !it.ch.empty()) v.push_back(it); return v; }();
    if(offset>=visible.size()) return;
    size_t end=std::min(offset+len, visible.size());
    int64_t ts=now_ms();
    for(size_t i=offset;i<end;i++){ for(auto &it: items_) if(it.id.counter==visible[i].id.counter && it.id.replica==visible[i].id.replica) { it.deleted=true; it.timestamp=ts; } }
    vector_[replica_id_]=std::max(vector_[replica_id_],ts);
}
bool RGAText::merge(const RGAText& o){
    bool changed=false;
    for(auto &oi: o.items_){
        auto it=std::find_if(items_.begin(), items_.end(), [&](auto& s){return s.id.counter==oi.id.counter && s.id.replica==oi.id.replica;});
        if(it==items_.end()){ items_.push_back(oi); changed=true; }
        else if(it->deleted!=oi.deleted && std::make_pair(oi.timestamp,oi.id.replica)>std::make_pair(it->timestamp,it->id.replica)){ it->deleted=oi.deleted; it->timestamp=oi.timestamp; changed=true; }
    }
    for(auto &kv:o.vector_) if(kv.second>vector_[kv.first]){ vector_[kv.first]=kv.second; changed=true; }
    return changed;
}
std::string RGAText::to_json() const { return "{\"type\":\"rga\",\"doc_id\":\""+doc_id_+"\",\"text\":\""+to_text()+"\"}"; }
RGAText RGAText::from_json(const std::string& s){ RGAText r("",""); r.set_text(s); return r; }
void RGAText::maybe_rebalance(){}
}
