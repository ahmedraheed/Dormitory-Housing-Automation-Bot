import pytz
from datetime import datetime, timedelta
from scheduler import in_active_window, seconds_until_next_window, window_end_time, BERLIN_TZ

now = datetime.now(BERLIN_TZ)
print("Current Berlin time:", now.strftime("%A %Y-%m-%d %H:%M %Z"))
print("In active window now:", in_active_window(now))

secs = seconds_until_next_window(now)
wake = now + timedelta(seconds=secs)
h = int(secs) // 3600
m = (int(secs) % 3600) // 60
print("Next window opens:", wake.strftime("%A %d.%m.%Y %H:%M Berlin"))
print("Time until:", str(h) + "h " + str(m) + "m")

# Test Monday 10:30 - should be IN window
days_until_mon = (0 - now.weekday()) % 7
if days_until_mon == 0:
    days_until_mon = 7
test_mon = (now + timedelta(days=days_until_mon)).replace(hour=10, minute=30, second=0)
print("Monday 10:30 in window:", in_active_window(test_mon), "(should be True)")

# Test Tuesday 13:00 - should be OUTSIDE window
days_until_tue = (1 - now.weekday()) % 7
if days_until_tue == 0:
    days_until_tue = 7
test_tue = (now + timedelta(days=days_until_tue)).replace(hour=13, minute=0, second=0)
print("Tuesday 13:00 in window:", in_active_window(test_tue), "(should be False)")

# Test Wednesday 11:00 - should be IN window
days_until_wed = (2 - now.weekday()) % 7
if days_until_wed == 0:
    days_until_wed = 7
test_wed = (now + timedelta(days=days_until_wed)).replace(hour=11, minute=0, second=0)
print("Wednesday 11:00 in window:", in_active_window(test_wed), "(should be True)")
