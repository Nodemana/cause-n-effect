-- Praetorian critiques store a flaw list. The list does not reject or rank a lead.

ALTER TABLE praetorian_reviews ADD COLUMN flaws TEXT NOT NULL DEFAULT '[]';
