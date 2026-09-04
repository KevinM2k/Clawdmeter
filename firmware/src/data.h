#pragma once
#include <Arduino.h>

struct UsageData {
    float session_pct;       // utilization 0-100 (5h window Pro/Max; spending % Enterprise)
    int session_reset_mins;  // minutes until reset
    float weekly_pct;        // 7-day utilization (Pro/Max only; 0 for Enterprise)
    int weekly_reset_mins;   // minutes until weekly reset (Pro/Max only)
    char status[16];         // "allowed", "limited", etc.
    bool chime;              // play the session-reset chime; false unless daemon opts in
    bool enterprise;         // true = Enterprise spending-limit account
    int time_pct;            // 0-100: fraction of billing period elapsed (Enterprise)
    int period_days;         // total billing period length in days (Enterprise)
    char reset_date[12];     // formatted reset date e.g. "Jul 1" (Enterprise)
    long clock_epoch;        // local wall-clock epoch (s) from daemon; 0 = not provided
    int  clock_fmt;          // 12 or 24 (hour format from daemon); defaults to 24
    char label[12];          // plan name e.g. "Team"; empty when the daemon sends one plan
    bool expired;            // token expired — the page says so instead of showing numbers
    int  scoped_pct;         // per-model limit e.g. Fable (-1 when the plan has none)
    int  scoped_reset_mins;  // minutes until the scoped window resets
    char scoped_name[10];    // scoped limit's model name e.g. "Fable"
    bool ok;                 // data parse succeeded
    bool valid;              // false until first successful parse
};

// The daemon may report several plans (one per Claude config dir); the usage
// screen shows one at a time and swipes between them.
#define MAX_PLANS 3

struct UsagePlans {
    UsageData plan[MAX_PLANS];
    int  count;              // plans actually present (>= 1 once valid)
    int  active;             // index of the plan currently being used
    bool valid;
};
