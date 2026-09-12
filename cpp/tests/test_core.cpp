#include "fragile/vault.hpp"
#include "fragile/crdt.hpp"
#include "fragile/tasks.hpp"
#include <cassert>
#include <iostream>

int main(){
    auto fm=fragile::parse_frontmatter("---\ntitle: test\n---\nhello");
    assert(fm.fields["title"]=="test");
    fragile::LWWDocument d("x","a"); d.set_text("hi"); assert(d.text()=="hi");
    fragile::RGAText r("x","a"); r.set_text("hi"); assert(r.to_text()=="hi");
    auto tasks=fragile::parse_tasks("- [ ] todo\n- [x] done", "/tmp/a.md");
    assert(tasks.size()==2);
    std::cout<<"core ok\n";
    return 0;
}
