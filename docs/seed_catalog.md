# Seed catalog verification

The specs in `src/supplygate/seed/catalog.yaml` were entered by hand from manufacturer spec sheets
and are marked `verified: false`. Before the project is published, check each product against the
manufacturer's page and set `verified: true`.

What to check per category:

- **CPU:** socket, cores, threads, base clock (P-core base for Intel hybrid CPUs), max boost clock,
  TDP / processor base power, supported memory, boxed MPN.
- **GPU:** reference / Founders Edition boost clock, VRAM size and type, total board power.
  MPNs are left empty because reference cards are sold under varying part numbers.
- **RAM:** kit capacity, module count, data rate, CAS latency, MPN.

Sources: amd.com product pages, intel.com ARK, nvidia.com and amd.com GPU spec pages, and the memory
vendors' product pages (corsair.com, gskill.com, kingston.com, crucial.com).

`reference_price_eur` is an approximate street price, used only to generate plausible synthetic
offer prices. It is not a claim about real prices.

The test suite checks internal consistency (threads vs cores, boost above base, valid VRAM sizes,
socket vs memory support, RAM name vs attributes), not correctness against the manufacturer.
