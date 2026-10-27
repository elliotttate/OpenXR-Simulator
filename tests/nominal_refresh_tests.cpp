#include "nominal_refresh.h"
#include <cstdio>
#include <initializer_list>
int main() {
 int failures=0, checks=0;
 auto check=[&](bool ok){++checks; if(!ok)++failures;};
 for(const char* bad : {"", "0", "-120", "29.9", "500.1", "nan", "inf", "120bad", "1e9999"}) check(perf::NominalRefreshHz(bad)==90.0);
 check(perf::NominalRefreshHz(nullptr)==90.0);
 for(double hz : {30.,72.,90.,120.,144.,500.}) { char text[32]; std::snprintf(text,sizeof(text),"%.0f",hz);check(perf::NominalRefreshHz(text)==hz);check(perf::NominalPeriodNs(hz)>0); }
 check(perf::NominalPeriodNs(120.)==8333333);
 std::printf("%d checks, %d failures\n",checks,failures); return failures?1:0;
}
