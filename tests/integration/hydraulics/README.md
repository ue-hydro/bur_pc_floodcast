# FLUXOS validation

`validation-profile.json` declares explicit checks, immutable fixture hashes and
read-only script mounts under `/checks`. `verify_fluxos.py` checks binary identity,
loading and analytical outputs; `verify_errors.py` exercises invalid inputs.
The scripts are not copied into production images and are not build inputs.

The case data and numerical expectations preserve the reviewed original bytes.
The technical provenance expectation distinguishes build and linked runtime
package inventories. Results/logs belong outside the checkout. Historical reports
remain archived; moving a profile does not transfer its earlier PASS to a new suite.
