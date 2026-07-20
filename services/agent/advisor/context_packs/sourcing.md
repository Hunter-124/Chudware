# Parts sourcing / library context pack
# Tools: part_search, part_get, part_place, part_source, parts_taxonomy,
#        bom_price, bom_rollup, request_quote, match_part_number, vendors_health

## Rules
1. Prefer catalog parts: part_search → part_get → part_place. Never hand-model fasteners, bearings, inserts, or standard hardware.
2. Verify dims from part_get / datasheet — never invent catalog numbers or thread lengths.
3. BOM cost: bom_rollup + bom_price; request_quote for buy parts when price missing.
4. match_part_number for OEM PN → library model; vendors_health before scrape/import.
5. Ledger every placed part_id + source URL/PN.
