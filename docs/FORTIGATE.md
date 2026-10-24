# FortiGate setup for Employee Connect

For the network admin / ISP. Goal: the internet can reach our server on TCP 80 and 443.

Facts
- Public IP: 14.96.223.90 (DNS `employeeconnect.online` already points here)
- Firewall: FortiGate 60F
- Server: `192.168.1.33` (fixed IP, give it a DHCP reservation or static address)
- Today TCP 443 on the public IP answers with the FortiGate's own certificate, and TCP 80 is closed.

Replace `wan1` and `lan` below with the real interface names (Network > Interfaces).

## 1. Free port 443 on the firewall itself

The FortiGate admin GUI or SSL-VPN is using 443 on the WAN address.

GUI: System > Settings > Administration Settings, set HTTPS port to 8443.
If SSL-VPN is used: VPN > SSL-VPN Settings, set Listen on Port to 10443.

CLI:
```
config system global
    set admin-sport 8443
end
config vpn ssl settings
    set port 10443
end
```
After this, the admin GUI is at https://14.96.223.90:8443. Restrict it with Trusted Hosts if possible.
Also check `config system interface` > `edit wan1` > `show` that `allowaccess` does not need https on the WAN for anything else.

## 2. Virtual IPs (port forwards)

GUI: Policy & Objects > Virtual IPs > Create New > Virtual IP.

| Name | Interface | External IP | Map to IP | Port forwarding | Protocol | External port | Map to port |
|---|---|---|---|---|---|---|---|
| ec-http | wan1 | 14.96.223.90 | `192.168.1.33` | on | TCP | 80 | 80 |
| ec-https | wan1 | 14.96.223.90 | `192.168.1.33` | on | TCP | 443 | 443 |

CLI:
```
config firewall vip
    edit "ec-http"
        set extintf "wan1"
        set extip 14.96.223.90
        set mappedip "192.168.1.33"
        set portforward enable
        set protocol tcp
        set extport 80
        set mappedport 80
    next
    edit "ec-https"
        set extintf "wan1"
        set extip 14.96.223.90
        set mappedip "192.168.1.33"
        set portforward enable
        set protocol tcp
        set extport 443
        set mappedport 443
    next
end
```

## 3. Firewall policy

GUI: Policy & Objects > Firewall Policy > Create New.
- Incoming interface: wan1
- Outgoing interface: lan
- Source: all
- Destination: ec-http, ec-https
- Service: HTTP, HTTPS
- Action: ACCEPT, NAT disabled
- Security profiles: IPS on; no SSL inspection (it would break the app's certificate pinning)
- Log allowed traffic: all sessions

CLI:
```
config firewall policy
    edit 0
        set name "ec-inbound"
        set srcintf "wan1"
        set dstintf "lan"
        set srcaddr "all"
        set dstaddr "ec-http" "ec-https"
        set action accept
        set schedule "always"
        set service "HTTP" "HTTPS"
        set ips-sensor "default"
        set logtraffic all
    next
end
```
Keep NAT off so the server sees real client IPs (needed for login rate limiting).

## 4. Outbound from the server

The server needs outbound HTTPS and DNS (Let's Encrypt, push notifications, image pulls). The normal LAN-to-WAN policy already covers this; check it is not blocked for the server's IP.

## 5. Hairpin (optional)

Phones on office Wi-Fi that use `https://employeeconnect.online` get the public IP. If the FortiGate does not loop that back to the server, either add a policy lan > lan to the VIPs, or set an internal DNS record `employeeconnect.online -> 192.168.1.33`.

## 6. Test (from outside the office, e.g. phone on mobile data)

```
curl -I http://employeeconnect.online      # expect a reply from nginx (a redirect to https)
curl -I https://employeeconnect.online     # expect nginx, not a FortiGate certificate
```
Or ask anyone outside to open both URLs. The certificate must not say "FortiGate". The server stack must be running first.

## What to send back

- Confirmation that 80 and 443 are forwarded to `192.168.1.33`
- The new admin port for the FortiGate
- Whether the ISP blocks inbound 80 (some do; if so, tell us and we will use DNS-based certificate issuing)

