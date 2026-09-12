#include "fragile/srs.hpp"
#include <regex>
#include <sstream>

namespace fragile {

Card schedule(Card c, int q){
    if(q<3){ c.reps=0; c.interval=1; }
    else {
        if(c.reps==0) c.interval=1;
        else if(c.reps==1) c.interval=6;
        else c.interval = int(c.interval * c.ease);
        c.ease = c.ease + (0.1 - (5-q)*(0.08+(5-q)*0.02));
        if(c.ease<1.3) c.ease=1.3;
        c.reps++;
    }
    c.due = std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count() + c.interval*86400;
    return c;
}

std::vector<Card> due_cards(const std::vector<Card>& all, int64_t now){
    std::vector<Card> r;
    for(auto &c: all) if(c.due<=now) r.push_back(c);
    return r;
}

std::vector<Card> parse_srs(const std::string& md){
    std::vector<Card> out;
    static std::regex re(R"(#srs\s*\nQ:\s*(.*)\nA:\s*(.*))");
    std::sregex_iterator it(md.begin(), md.end(), re), end;
    for(; it!=end; ++it){ Card c; c.front=(*it)[1].str(); c.back=(*it)[2].str(); c.id=std::to_string(out.size()); out.push_back(c); }
    return out;
}

}
