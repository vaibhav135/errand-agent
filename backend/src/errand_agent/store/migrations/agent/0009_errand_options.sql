-- The ranked options last shown for a book errand (JSON of workflow.ranking.RankedOptions),
-- so "the second one" refers to the same option on the next turn.
ALTER TABLE errands ADD COLUMN options TEXT;
