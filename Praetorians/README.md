# Praetorians

Praetorians critique theses, and that is their only job. After the Centurions write individual theses, the Legate merges linked theses. A thesis the Legate does not merge gets no second pass and comes straight here from that first thesis. A merged finding comes here only after a Centurion writes a second, fresh full thesis on that merged lead. The critique runs before ranking.

They receive the Centurion's output: the recommendation, the expected return, the risks, the timeframe, and the stated assumptions. They write every flaw they find. They do not reject a lead, drop a lead, or remove it from the set that ranking sees. A thesis with a long flaw list is ranked on the same terms as a thesis with an empty list.

Ranking may show the flaw list. The score does not use it. The candidate report keeps every thesis. The Legate's later selection is a separate step.

## Output

One JSON object:

```json
{
  "flaws": [
    {
      "flaw": "The price-run classification is priced-in, so the claim that this is still early does not hold.",
      "severity": "high",
      "evidence": "The stored text marks the run as priced-in."
    }
  ]
}
```

`severity` is `low`, `moderate`, or `high`. `flaw` and `evidence` are non-empty strings. An empty `flaws` list means no flaw was recorded. The lead still goes to ranking.
