# Redfish mockups

Captured Redfish services, replayed offline so the hardware tier can run where no hardware is attached.

## What is here

These are from DMTF's published mockup bundle, DSP2043 2026.1, redistributed under the DMTF copyright policy
recorded in each resource. They describe fictional hardware (`Contoso`, `Chipwise`), so there is nothing here from
a real machine and no reason to keep them private.

| bundle | why it is kept |
|---|---|
| `public-rackmount1` | an ordinary server, the baseline |
| `public-liquid-cooled-server` | a server with cooling resources attached |
| `public-bladed` | several systems behind one manager |
| `public-pdu` | a service with no `Systems` collection at all |
| `public-power-shelf` | power equipment |
| `public-cooling-unit` | a cooling distribution unit |

The last four matter most, because they are shapes a normal BMC never presents. Pointing confluent at them
immediately produced two results worth having: `public-bladed` is correctly refused as a multi system manager
needing an explicit system url, a disambiguation path nothing else exercises, and `public-pdu` raises
`TypeError: Constructor parameter should be str` from inside the url library rather than saying it has no
systems, which reaches a user as "Unexpected Error".

What they cannot do is exercise a vendor OEM handler: `Contoso` is not a vendor confluent knows, so every bundle
takes the generic path. Vendor coverage needs a capture of a real machine, which carries that machine's identity
and belongs somewhere private.

The specification documents each bundle ships (`JsonSchemas`, `schemas`, `metadata`, `$metadata`, `odata`) have
been removed. Confluent never requests them, they are identical across vendors, and they were most of the size.

## Serving one

Normally nothing needs to be: an inventory entry carrying `mockup: <name>` has the `redfish_mockups` fixture serve
it for the run. What follows is for looking at one by hand, and a service already answering on the port is left
alone by the fixture, so a server started this way is used rather than replaced.

These use the short form layout, without the `/redfish/v1` prefix, so the server needs `-S`. A capture taken with
`tests/support/capture-mockups.py` does not.

```sh
podman run -d -p 8451:8000 --security-opt label=disable \
    -v $PWD/tests/support/mockups/public-rackmount1:/mockup:ro -v <certs>:/certs:ro \
    docker.io/dmtf/redfish-mockup-server:latest \
    -D /mockup -S -s --cert /certs/cert.pem --key /certs/key.pem
```

Certificates can be any self signed pair; confluent talks https only and the tier does not verify them.

`inventory-dmtf.yaml` beside this directory maps the bundles onto ports and records what each cannot satisfy.

## Refreshing them

DSP2043 is published at <https://www.dmtf.org/dsp/DSP2043>. The download sits behind bot protection, so fetch the
zip with a browser rather than from a script, and do not add a download step to the tests: it would make every run
depend on someone else's web server.
