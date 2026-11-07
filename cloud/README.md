# AstraBox cloud images

This directory builds the AstraBox cloud image and holds what each cloud
marketplace needs to list it. The user-facing guide is
[docs/cloud-images.md](../docs/cloud-images.md).

| Path | What it is |
|---|---|
| `image/build.sh` | Turns a fresh Ubuntu 24.04 machine into the image: Docker, one release's bundle, every container image that release runs, the first-boot service. |
| `image/cleanup.sh` | Removes the build machine's keys, logs, history and identity. Run it last. |
| `image/rootfs/` | Files `build.sh` installs: `astrabox-first-boot` and its systemd unit, `astrabox-cloud`, the login message, an example `cloud.env`. |
| `packer/astrabox.pkr.hcl` | Runs both scripts on AWS, DigitalOcean, Alibaba Cloud, Tencent Cloud and Huawei Cloud. |
| `computenest/` | An Alibaba Cloud Compute Nest service: one ECS instance from the image, with a model service and optional domains as deployment parameters. |

## How the image works

`build.sh` installs the release once, with team login, to collect exactly the
images an installation runs. It then asks that running server which images it
starts beside each sandbox (execd, the egress sidecar and the workspace
helper), pulls them, and removes the installation again with its volumes,
secrets and settings. What remains is Docker's image store, the bundle and the
installer in `/opt/astrabox-image`, and the first-boot service.

On a machine's first boot, `astrabox-first-boot` runs the installer from the
local bundle with `ASTRABOX_INSTALL_PULL=missing`, team login on, and the
machine's public address as the console's and Casdoor's origins. It then starts
Caddy on ports 80, 443 and 8443. Caddy 2.11 requests Let's Encrypt's
short-lived IP address certificates. A machine with no route to a registry or
to GitHub still starts.

Tested on a Docker host by running `build.sh`'s steps and the first boot. That
test covered the images collected, a first boot with no image pulls, the
installer with the local bundle, the HTTPS edge with a self-signed
certificate, and a browser sign-in through Casdoor on port 8443 back to the
console. Not tested yet: a Let's Encrypt IP certificate on a real public
address, and the Packer sources and the Compute Nest template against real
cloud accounts.

## Build

With Packer 1.10 or later, and the cloud's credentials in its usual environment
variables (listed at the top of the template):

```bash
cd cloud/packer
packer init .
packer build -only='amazon-ebs.astrabox' -var astrabox_version=0.1.14 .
```

Sources: `amazon-ebs.astrabox`, `digitalocean.astrabox`,
`alicloud-ecs.astrabox`, `tencentcloud-cvm.astrabox`,
`huaweicloud-ecs.astrabox`. Variables worth knowing:

| Variable | Use |
|---|---|
| `astrabox_version` | The release to bake. Required. |
| `disk_size_gb` | Default 60. A machine from the image needs at least this disk. |
| `warm_engines` | Also bake the sandbox images of `codex`, `pi`, `deepseek-harness` or `hermes`; each adds several GiB. |
| `image_prefix` | A registry mirror that holds the AstraBox images, such as an Alibaba Cloud Container Registry namespace. The image keeps it, so later upgrades pull from it as well. |
| `docker_install_mirror`, `docker_registry_mirrors` | For build machines in mainland China, where Docker's download site and Docker Hub are slow or blocked. |

On a cloud without a Packer source (Volcengine, Vultr and others), start an
Ubuntu 24.04 machine with a 60 GiB disk, copy `cloud/image/` and
`scripts/install.sh` to it, run
`sudo ASTRABOX_VERSION=<version> ./build.sh && sudo ./cleanup.sh`, stop it, and
create an image from it in the console.

Rebuild the image for every AstraBox release you want listed. The installer in
the image must be one that knows `ASTRABOX_INSTALL_PULL`; `build.sh` refuses
an older one.

## Publish

Every marketplace needs an account in the publisher's name, and most need a
legal entity, tax and support details. Those steps are the publisher's to do;
everything the image and the listing need technically is in this directory.
Requirements change, so check each program's current terms before you apply.

### AWS Marketplace (AMI product, free)

1. Register as a seller in the AWS Marketplace Management Portal. A seller
   offering only free products does not need to give banking details. The
   portal lists the countries it accepts sellers from.
2. Build with `amazon-ebs.astrabox` in `us-east-1`. The AMI requires IMDSv2,
   has no password login and no default credentials. Each machine generates
   its own credentials.
3. In the portal, create a single-AMI product. Give AWS Marketplace access to
   the AMI through the ingestion role the portal describes, and add the AMI
   as a version.
4. Listing details:
   - Usage instructions: run `sudo astrabox-cloud info`, then open the address
     it shows.
   - Security group: inbound TCP 80, 443 and 8443 from 0.0.0.0/0, and 22 for
     SSH.
   - Recommended instance type: `t3.large` or larger (2 vCPUs, 8 GiB).
5. Test the product while it is Limited, then request Public visibility.

### DigitalOcean Marketplace (Droplet 1-Click App)

1. Apply as a vendor at marketplace.digitalocean.com/vendors.
2. Build with `digitalocean.astrabox`. The build runs DigitalOcean's own image
   check (`99-img-check.sh`) after `cleanup.sh` and fails if the image does
   not pass. The build Droplet is `s-2vcpu-4gb` because a Droplet made from the
   snapshot needs at least the build Droplet's disk.
3. Submit the snapshot in the vendor portal, with the getting-started text from
   [docs/cloud-images.md](../docs/cloud-images.md#sign-in) and a recommended
   size of at least 8 GiB of memory.

### Alibaba Cloud Compute Nest (计算巢)

Compute Nest charges nothing to publish a service; deploying users pay for the
ECS instance. To publish a public service, the account must be an enterprise
account approved as a service provider in the Compute Nest console. An
individual account with identity verification can create services for its own
use.

1. Build with `alicloud-ecs.astrabox`. For a build in a mainland region, set
   `image_prefix` to a Container Registry namespace that holds the AstraBox
   images, and set `docker_install_mirror=Aliyun` and a Docker Hub mirror.
2. In the Compute Nest console, create an artifact of type ECS image from that
   image, and let it be copied to the regions the service is offered in.
3. Put the artifact ID in `computenest/config.yaml`
   (`ArtifactRelation.astrabox.ArtifactId`).
4. Create the service from the `computenest/` directory, either with
   `computenest-cli` or by pasting `config.yaml` and the template into the
   console. Test-deploy it, then submit it for review.
5. Optional: apply for Compute Nest's free trial center, where Alibaba Cloud
   provides the trial's resources.

The template creates a VPC, a security group with 80, 443 and 8443 open, and one
instance. It passes the model service and optional domains to the first boot as
user data. A `RunCommand` waits for the first boot, so the deployment fails
with the setup's own error message if setup fails. The deployment's output is
the console address. The admin password stays on the instance
(`sudo astrabox-cloud info`, for example in ECS Workbench).

### Other clouds

| Marketplace | Image | Listing |
|---|---|---|
| Alibaba Cloud Marketplace (云市场), image product | `alicloud-ecs.astrabox` | Enterprise service provider registration. |
| Tencent Cloud Marketplace (云市场), image product | `tencentcloud-cvm.astrabox` | Service provider registration. Lighthouse application templates are offered through Tencent Cloud's partner program, not a self-service listing. |
| Huawei Cloud KooGallery | `huaweicloud-ecs.astrabox` | Enterprise seller registration. The image must be a private image the seller created, not one shared with them. |
| Volcengine Marketplace | Build by hand (see [Build](#build)) | Service provider registration. |
| Akamai (Linode) Marketplace | Not an image: apps are Ansible playbooks contributed to Akamai's marketplace repository | Partner application. The playbook can run `build.sh` and the first boot on the new Linode. |
| Google Cloud, Azure | Their image formats via Packer's `googlecompute` and `azure-arm` builders, not yet in the template | Partner programs: Google Cloud Partner Advantage and Microsoft Partner Center. |

## Checklist before each submission

- The image boots on the smallest recommended size and `sudo astrabox-cloud
  info` shows `trusted` for the certificate on a public address.
- Sign-in as `admin` works, and a conversation with a model service starts a
  sandbox without pulling an image (`docker events --filter type=image` shows
  no pull).
- `cloud.env` contains no API key after the first boot.
- No SSH key, shell history or log from the build is left on the image
  (`cleanup.sh`).
