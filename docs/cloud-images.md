# Run AstraBox from a cloud image

> Start AstraBox on a cloud machine from the AstraBox image. The first boot sets up team login and HTTPS.

The AstraBox cloud image is Ubuntu 24.04 with Docker, one AstraBox release and
every container image that release runs. A machine started from it installs
AstraBox on its first boot with its own secrets, so you do not run the
installer or answer its questions:

1. It reads optional settings from `/etc/astrabox/cloud.env`, such as a model
   service. You can pass them as user data when you start the machine.
2. It finds the machine's public IPv4 address.
3. It installs AstraBox from the image with [team login](team-login.md) on.
   The console is never served without sign-in.
4. It starts [Caddy](https://caddyserver.com/) in front of AstraBox. Caddy
   serves the console at `https://<public IP>` and the sign-in pages at
   `https://<public IP>:8443`, with a Let's Encrypt certificate for the IP
   address.
5. It writes the sign-in details to `/root/astrabox-credentials.txt`.

The first boot pulls nothing from a container registry and needs no GitHub
access. It takes a few minutes.

## Requirements

- **Machine size**: at least 2 vCPUs and 8 GiB of memory. With no
  conversation running, AstraBox and its services use about 2 GiB. Each Agent
  keeps one sandbox ready, about 0.5 GiB each, and every conversation runs in
  its own sandbox of up to 1 vCPU and 1 GiB. See
  [Plan capacity for prepared sandboxes](deploy.md#plan-capacity-for-prepared-sandboxes).
- **Disk**: at least 60 GiB. The images take about 13 GiB unpacked, and
  conversation workspaces and the database grow from there.
- **Inbound ports**: 80, 443 and 8443 from the internet. Let's Encrypt checks
  the address on port 80. Port 22 is needed only for SSH.
- **A public IPv4 address that does not change**, such as an elastic IP
  address. See [If the address changes](#if-the-address-changes).

## Start a machine

Start the machine from the AstraBox image in your cloud's marketplace, or from
an image you built (see [Build the image](#build-the-image)). Open the ports
above in its security group or firewall.

Every setting is optional. To set some, pass user data that writes
`/etc/astrabox/cloud.env`. For example, this connects DeepSeek:

```yaml
#cloud-config
write_files:
  - path: /etc/astrabox/cloud.env
    permissions: '0600'
    content: |
      ASTRABOX_INSTALL_MODEL_PROVIDER=deepseek
      ASTRABOX_INSTALL_MODEL_API_KEY=your-deepseek-api-key
```

| Setting | Meaning |
|---|---|
| `ASTRABOX_INSTALL_MODEL_PROVIDER` | `anthropic`, `deepseek`, `anthropic-compatible`, `openai-compatible` or `none` (default). With `none`, add a model service later in the console. |
| `ASTRABOX_INSTALL_MODEL_API_KEY` | The model service's API key. The first boot moves it into the installation's settings, which only root can read, and removes it from `cloud.env`. |
| `ASTRABOX_INSTALL_MODEL_NAME` | The model ID the default Agents use. Default for DeepSeek: `deepseek-flash`. |
| `ASTRABOX_INSTALL_MODEL_BASE_URL` | The base URL of an Anthropic-compatible or OpenAI-compatible service. |
| `ASTRABOX_CONSOLE_DOMAIN`, `ASTRABOX_LOGIN_DOMAIN` | Two host names whose DNS records point at the machine: one for the console and one for the sign-in pages. Set both or neither. With them, the console is at `https://<console domain>` and port 8443 is not used. |
| `ASTRABOX_PUBLIC_ADDRESS` | The IPv4 address browsers use, if the machine cannot find its public address itself. |
| `ASTRABOX_TLS` | `acme` (default): a Let's Encrypt certificate, with a self-signed one as a fallback. `internal`: only a self-signed certificate. |
| `ASTRABOX_ACME_EMAIL` | The email address Let's Encrypt sends certificate notices to. |

User data is stored with the machine's configuration in your cloud account,
so anyone who can read that configuration can read the API key there. To keep
the key out of it, leave the model unset and add the model service in the
console after you sign in.

## Sign in

When the first boot finishes, the login message on the machine shows the
console's address. To show the sign-in details, run:

```bash
sudo astrabox-cloud info
```

Open the console address and sign in as `admin` with the password shown. To
invite other people, create their accounts in Casdoor's management console, as
the same output describes. Casdoor's management console is reachable only from
the machine itself, through an SSH tunnel.

If you did not set a model service, open **Console > Integrated services >
LiteLLM gateway** and add one. See [Connect a model](models.md).

To follow the first boot, or to find out why it failed:

```bash
sudo astrabox-cloud status
sudo astrabox-cloud logs
```

After you fix the cause of a failure, run
`sudo systemctl restart astrabox-first-boot`. It keeps whatever an earlier
attempt generated.

## The certificate

Let's Encrypt issues certificates for IP addresses only with a lifetime of
about six days. Caddy renews the certificate automatically before it expires.
To issue it, Let's Encrypt must reach the machine on port 80.

If Let's Encrypt cannot validate the address, for example because port 80 is
closed, Caddy serves a self-signed certificate instead, and browsers show a
warning. Open the ports, then run `sudo docker restart astrabox-edge` to try
again.

In mainland China regions, cloud providers block HTTP and HTTPS requests to a
host name that has no ICP filing. Requests to the IP address are not blocked.
Use `ASTRABOX_CONSOLE_DOMAIN` and `ASTRABOX_LOGIN_DOMAIN` there only with
filed domains.

## If the address changes

The first boot registers the console's address with Casdoor as the address it
returns browsers to after sign-in. If the public IP address changes, for
example after the machine is stopped and started without an elastic IP
address, sign-in fails. Give the machine a static address before you invite
users.

To move AstraBox to a new address or to host names:

1. In Casdoor's management console, open **Applications >
   astrabox-console** and add the new callback URL,
   `https://<new console address>/api/v1/auth/callback`, to **Redirect URLs**.
2. Set the new addresses in `/etc/astrabox/cloud.env`
   (`ASTRABOX_PUBLIC_ADDRESS`, or the two domain settings).
3. Run the first boot again:

   ```bash
   sudo rm /var/lib/astrabox-cloud/first-boot.done
   sudo systemctl restart astrabox-first-boot
   ```

## Upgrade

Run the installer from the main branch against the installation the first boot
created:

```bash
curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh \
  | sudo ASTRABOX_INSTALL_DIR=/opt/astrabox bash
```

It keeps the secrets, the database, team login and the public addresses. The
HTTPS edge is not changed.

## Build the image

The image is built by two scripts in the repository, `cloud/image/build.sh` and
`cloud/image/cleanup.sh`. A Packer template, `cloud/packer/astrabox.pkr.hcl`,
runs them on AWS, DigitalOcean, Alibaba Cloud, Tencent Cloud and Huawei Cloud.
On another cloud, start an Ubuntu 24.04 machine, run both scripts as root, and
create an image from the stopped machine. `cloud/README.md` describes the build
settings and how to publish the image on each cloud's marketplace.

## Related guides

- [Deploy AstraBox](deploy.md)
- [Set up team login](team-login.md)
- [Connect a model](models.md)
