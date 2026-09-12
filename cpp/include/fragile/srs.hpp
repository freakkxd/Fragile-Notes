#pragma once
#include <string>
#include <vector>
#include <chrono>

namespace fragile {

struct Card {
    std::string id;
    std::string front;
    std::string back;
    double ease = 2.5;
    int interval = 0;
    int reps = 0;
    int64_t due = 0;
};

Card schedule(Card c, int quality);
std::vector<Card> due_cards(const std::vector<Card>& all, int64_t now);
std::vector<Card> parse_srs(const std::string& markdown);

}
