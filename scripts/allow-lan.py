#!/usr/bin/env python3
"""Let agent tasks reach the home network: add every host address on this Mac's subnets to
allowedDomains in ~/.night-elves/agent-settings.json. Run it again after the network changes;
--remove takes the entries out. The sandbox has no CIDR syntax, hence one entry per host.

Only TCP gets through, and only via the sandbox proxy (NO_PROXY lists the private ranges, so
tools have to be pointed at the proxy). Ping, UDP, mDNS and packet capture still can't."""
import argparse, ipaddress, json, pathlib, re, subprocess

SETTINGS = pathlib.Path.home() / ".night-elves" / "agent-settings.json"
MAX_HOSTS = 4094  # refuse anything bigger than a /20

def subnets():
    out = subprocess.run(["ifconfig"], capture_output=True, text=True, check=True).stdout
    for addr, mask in re.findall(r"inet (\d+\.\d+\.\d+\.\d+) netmask 0x([0-9a-f]{8})", out):
        ip = ipaddress.ip_address(addr)
        if ip.is_private and not ip.is_loopback and not ip.is_link_local:
            yield ip, ipaddress.ip_network(f"{addr}/{bin(int(mask, 16)).count('1')}", strict=False)

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--settings", type=pathlib.Path, default=SETTINGS)
    args = ap.parse_args()
    s = json.loads(args.settings.read_text())
    net = s["sandbox"]["network"]
    record = args.settings.with_name("lan-hosts.json")  # Claude rejects unknown settings keys
    lan = set(json.loads(record.read_text())) if record.exists() else set()
    domains = [d for d in net["allowedDomains"] if d not in lan]
    hosts = set()
    if not args.remove:
        found = list(subnets())
        own = {ip for ip, _ in found}
        for ip, n in found:
            if n.num_addresses > MAX_HOSTS + 2:
                raise SystemExit(f"{n} is too big to list host by host")
            hosts.update(h for h in n.hosts() if h not in own)
            print(f"allowing {n} (this Mac is {ip})")
    added = [str(h) for h in sorted(hosts)]
    net["allowedDomains"] = domains + added
    args.settings.write_text(json.dumps(s, indent=2) + "\n")
    record.write_text(json.dumps(added))
    print(f"{len(added)} LAN entries in {args.settings}")

if __name__ == "__main__":
    main()
