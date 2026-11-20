# Data-quality report - Stock Rise Estimation

## Ingestion (FR-1.5)

- total lines in file: 94838
- banner / blank lines skipped: 3
- header lines (blocks): 14
- data rows: 94821

## Tables (rows in -> rows out)

| table | rows in | rows out | nulls out | columns |
|---|---|---|---|---|
| inventory_daily | 43800 | 43800 | 0 | 13 |
| calendar | 1096 | 1096 | 0 | 5 |
| customers | 120 | 120 | 0 | 4 |
| products | 120 | 120 | 0 | 12 |
| product_supplier | 306 | 306 | 0 | 5 |
| purchase_orders | 3159 | 3159 | 0 | 16 |
| sales | 42204 | 42204 | 0 | 14 |
| channels | 4 | 4 | 0 | 5 |
| events | 153 | 153 | 0 | 7 |
| suppliers | 12 | 12 | 0 | 5 |
| routes | 28 | 28 | 0 | 8 |
| weather | 3285 | 3285 | 0 | 7 |
| abc_class | 534 | 267 | 0 | 5 |

## Rule results

| rule | table | checked | violations | action |
|---|---|---|---|---|
| FR-4.3 | abc_class | 267 | 0 | duplicate block dropped |
| FR-3.3 | inventory_daily | 43800 | 0 | none |
| FR-4.1 | inventory_daily | 43800 | 0 | none |
| FR-4.2 | inventory_daily | 43800 | 0 | none |
| FR-4.4 | inventory_daily | 43800 | 0 | none |
| FR-5.1 | inventory_daily | 43800 | 0 | none |
| FR-5.2 | inventory_daily | 43800 | 0 | flagged |
| FR-6.3 | inventory_daily | 43800 | 1238 | labelled as stock-out |
| FR-6.1 | inventory_daily.on_hand_qty | 43800 | 3913 | flagged |
| FR-6.1 | inventory_daily.in_transit_qty | 43800 | 1847 | flagged |
| FR-6.1 | inventory_daily.reserved_qty | 43800 | 1108 | flagged |
| FR-3.3 | abc_class | 267 | 0 | none |
| FR-4.1 | abc_class | 267 | 0 | none |
| FR-4.2 | abc_class | 267 | 0 | none |
| FR-5.8 | abc_class | 267 | 0 | none |
| FR-5.8 | abc_class | 267 | 0 | none |
| FR-5.8 | abc_class | 267 | 0 | none |
| FR-3.3 | calendar | 1096 | 0 | none |
| FR-4.1 | calendar | 1096 | 0 | none |
| FR-4.2 | calendar | 1096 | 0 | none |
| FR-7.2 | calendar.is_weekend | 1096 | 0 | reported |
| FR-3.3 | products | 120 | 0 | none |
| FR-4.1 | products | 120 | 0 | none |
| FR-4.2 | products | 120 | 0 | none |
| FR-7.2 | products | 120 | 0 | reported |
| FR-5.3 | products | 120 | 0 | none |
| FR-5.4 | products | 120 | 0 | reported |
| FR-3.3 | sales | 42204 | 0 | none |
| FR-4.1 | sales | 42204 | 0 | none |
| FR-4.2 | sales | 42204 | 0 | none |
| FR-5.1 | sales | 42204 | 0 | none |
| FR-5.3 | sales | 42204 | 0 | none |
| FR-5.3 | sales | 42204 | 0 | none |
| FR-6.1 | sales.qty | 42204 | 543 | flagged |
| FR-6.1 | sales.discount_rate | 42204 | 136 | flagged |
| FR-3.3 | purchase_orders | 3159 | 0 | none |
| FR-4.1 | purchase_orders | 3159 | 0 | none |
| FR-4.2 | purchase_orders | 3159 | 0 | none |
| FR-5.1 | purchase_orders | 3159 | 0 | none |
| FR-5.3 | purchase_orders | 3159 | 0 | none |
| FR-5.5 | purchase_orders | 3159 | 0 | none |
| FR-5.6 | purchase_orders | 3159 | 0 | reported |
| FR-5.7 | purchase_orders | 3159 | 324 | flagged (kept) |
| FR-6.1 | purchase_orders.qty_ordered | 3159 | 672 | flagged |
| FR-6.1 | purchase_orders.qty_received | 3159 | 627 | flagged |
| FR-6.1 | purchase_orders.unit_cost | 3159 | 34 | flagged |
| FR-3.3 | channels | 4 | 0 | none |
| FR-4.1 | channels | 4 | 0 | none |
| FR-4.2 | channels | 4 | 0 | none |
| FR-3.2 | channels.ship_mode | 4 | 1 | filled with label 'None' |
| FR-3.3 | events | 153 | 0 | none |
| FR-4.1 | events | 153 | 0 | none |
| FR-4.2 | events | 153 | 0 | none |
| FR-3.3 | weather | 3285 | 0 | none |
| FR-4.1 | weather | 3285 | 0 | none |
| FR-4.2 | weather | 3285 | 0 | none |
| FR-5.9 | weather | 3285 | 0 | none |
| FR-5.1 | weather | 3285 | 0 | none |
| FR-6.1 | weather.temp_c | 3285 | 0 | flagged |
| FR-6.1 | weather.precip_mm | 3285 | 538 | flagged |
| FR-3.3 | customers | 120 | 0 | none |
| FR-4.1 | customers | 120 | 0 | none |
| FR-4.2 | customers | 120 | 0 | none |
| FR-3.3 | product_supplier | 306 | 0 | none |
| FR-4.1 | product_supplier | 306 | 0 | none |
| FR-4.2 | product_supplier | 306 | 0 | none |
| FR-3.3 | suppliers | 12 | 0 | none |
| FR-4.1 | suppliers | 12 | 0 | none |
| FR-4.2 | suppliers | 12 | 0 | none |
| FR-3.3 | routes | 28 | 0 | none |
| FR-4.1 | routes | 28 | 0 | none |
| FR-4.2 | routes | 28 | 0 | none |
| FR-8.1 | sales | 42204 | 0 | none |
| FR-8.1 | sales | 42204 | 0 | none |
| FR-8.1 | sales | 42204 | 0 | none |
| FR-8.1 | purchase_orders | 3159 | 0 | none |
| FR-8.1 | purchase_orders | 3159 | 0 | none |
| FR-8.1 | purchase_orders | 3159 | 0 | none |
| FR-8.1 | product_supplier | 306 | 0 | none |
| FR-8.1 | product_supplier | 306 | 0 | none |
| FR-8.1 | routes | 28 | 0 | none |
| FR-8.1 | abc_class | 267 | 0 | none |
| FR-8.1 | inventory_daily | 43800 | 0 | none |
| FR-7.3 | routes.shipment_cost | 28 | 0 | no currency column in source - assumed base currency |

## Output

- panel rows: 43800, products: 120, days: 365
- stock_rise nulls (first day per product): 120
- rejected rows: 0
