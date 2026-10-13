-- Closes used by the thesis price table. The last price stays the official
-- quote. These columns are the prior-year close, the close 52 weeks earlier,
-- and the 52-week low and high when the agent or the Markit header supplied
-- them. A blank column is not a guess.

ALTER TABLE prices ADD COLUMN prior_year_close REAL;
ALTER TABLE prices ADD COLUMN close_1y REAL;
ALTER TABLE prices ADD COLUMN low_52w REAL;
ALTER TABLE prices ADD COLUMN high_52w REAL;
