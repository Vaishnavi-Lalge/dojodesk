-- Starter belt system. Everything here (names, order, colours, requirements) is editable
-- in the app under Belts. rank_order uses gaps of 10 so new ranks can be inserted in between.
-- min_months / min_attendance_pct are ADVISORY eligibility hints only; promotions always need
-- an instructor's explicit approval.
INSERT INTO belt_ranks (name, rank_order, color_hex, min_months, min_attendance_pct) VALUES
 ('White Belt',   10, '#F1F5F9', 0, 0),
 ('Yellow Belt',  20, '#FACC15', 3, 75),
 ('Orange Belt',  30, '#FB923C', 3, 75),
 ('J Green Belt', 40, '#4ADE80', 3, 75),
 ('S Green Belt', 50, '#16A34A', 3, 75),
 ('Blue Belt',    60, '#3B82F6', 4, 75),
 ('3 Brown Belt', 70, '#A16207', 6, 75),
 ('2 Brown Belt', 80, '#854D0E', 6, 75),
 ('1 Brown Belt', 90, '#713F12', 6, 75),
 ('Black Belt',  100, '#111827', 12, 80)
ON CONFLICT (name) DO NOTHING;
