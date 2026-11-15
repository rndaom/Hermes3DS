// The bottom-screen tabs other than Chat: Bots, Sessions, Skills, Jobs and More (commands, model picker,
// status). Each fetches its list from the plugin (protocol.md §4.6) when opened.
#pragma once

#include <3ds.h>
#include <jansson.h>
#include <stdbool.h>

#include "app.h"

void tabs_switch(Tab t);  // also used to return to Chat
void tabs_online(void);   // connected again: lists are stale
void tabs_set_jobs_edit(bool yes);  // the PC can make, change and delete jobs (welcome `jobs.edit`)
void tabs_set_sessions_edit(bool yes);  // the PC can rename and delete sessions (welcome `sessions.edit`)
void tabs_set_switch(bool skills, bool tools);  // the PC can turn skills / tools on and off (welcome `switch`)
void tabs_agent_changed(void);  // another agent was selected: its own sessions, skills, jobs and models
// A reply to one of the tabs' requests. Returns true when it was consumed (status_r is only copied).
bool tabs_handle(json_t *msg);
void tabs_input(u32 down, u32 held, const touchPosition *touch);
bool tabs_animating(void);
void tabs_draw_top(void);
void tabs_draw_bottom(void);
