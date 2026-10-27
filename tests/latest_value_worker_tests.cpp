#include "latest_value_worker.h"
#include <atomic>
#include <chrono>
#include <cstdio>
#include <future>
#include <vector>
using namespace std::chrono_literals;
int main() {
    int checks=0, failures=0;
    auto check=[&](bool pass,const char* what){++checks;if(!pass){++failures;std::fprintf(stderr,"FAIL: %s\n",what);}};
    perf::LatestValueWorker<int> worker;
    check(!worker.Submit(0),"inactive worker rejects snapshots");
    std::promise<void> entered, release;
    auto ready=entered.get_future();auto gate=release.get_future().share();
    std::vector<int> seen;
    worker.Start([&](const int& value){seen.push_back(value);if(value==1){entered.set_value();gate.wait();}});
    auto first=std::async(std::launch::async,[&]{return worker.Submit(1);});
    check(ready.wait_for(2s)==std::future_status::ready,"writer enters controlled blocked sink");
    auto producer=std::async(std::launch::async,[&]{for(int i=2;i<=5000;++i)if(!worker.Submit(i))return false;return true;});
    check(producer.wait_for(500ms)==std::future_status::ready,"submissions finish while sink stays blocked");
    // Stop must not return with code still executing in a DLL worker.
    auto stop=std::async(std::launch::async,[&]{worker.Stop();});
    check(stop.wait_for(30ms)==std::future_status::timeout,"shutdown joins blocked in-flight writer");
    release.set_value();
    check(first.get() && producer.get(),"accepted snapshots");
    check(stop.wait_for(2s)==std::future_status::ready,"shutdown drains newest snapshot and joins");stop.get();
    check(seen.size()==2 && seen.front()==1 && seen.back()==5000,"bounded queue coalesces to newest pending snapshot");
    check(!worker.Submit(5001),"stopped worker rejects new snapshots");
    worker.Stop();
    worker.Start([&](const int& value){seen.push_back(value);});
    check(worker.Submit(7000),"new instance can restart worker");worker.Stop();
    check(seen.size()==3 && seen.back()==7000,"restart has no stale work and flushes final snapshot");
    std::printf("%d checks, %d failures\n",checks,failures);return failures?1:0;
}
