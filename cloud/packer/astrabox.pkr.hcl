# Builds the AstraBox cloud image on each cloud from Ubuntu 24.04:
#
#   cd cloud/packer
#   packer init .
#   packer build -only='amazon-ebs.astrabox' -var astrabox_version=0.1.14 .
#
# Every source runs the same two scripts, cloud/image/build.sh and
# cloud/image/cleanup.sh; only the base image, the build machine and the
# credentials differ. Credentials come from each plugin's usual environment
# variables, never from this file:
#   AWS            AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (or a profile)
#   DigitalOcean   DIGITALOCEAN_TOKEN
#   Alibaba Cloud  ALICLOUD_ACCESS_KEY / ALICLOUD_SECRET_KEY
#   Tencent Cloud  TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY
#   Huawei Cloud   HW_ACCESS_KEY / HW_SECRET_KEY
# See cloud/README.md for what each marketplace then asks for.

packer {
  required_version = ">= 1.10.0"
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = "~> 1"
    }
    digitalocean = {
      source  = "github.com/digitalocean/digitalocean"
      version = "~> 1"
    }
    alicloud = {
      source  = "github.com/hashicorp/alicloud"
      version = "~> 1"
    }
    tencentcloud = {
      source  = "github.com/hashicorp/tencentcloud"
      version = "~> 1"
    }
    huaweicloud = {
      source  = "github.com/huaweicloud/huaweicloud"
      version = "~> 1"
    }
  }
}

# ── what goes into the image ─────────────────────────────────────────────────

variable "astrabox_version" {
  type        = string
  description = "The AstraBox release baked into the image, such as 0.1.14."
}

variable "image_prefix" {
  type        = string
  default     = ""
  description = "Registry mirror holding the AstraBox images (ASTRABOX_IMAGE_PREFIX). Empty: ghcr.io."
}

variable "warm_engines" {
  type        = string
  default     = ""
  description = "Sandbox images to bake besides Claude Code's: codex pi deepseek-harness hermes."
}

variable "disk_size_gb" {
  type        = number
  default     = 60
  description = "Root disk of the build machine, and so the smallest disk a machine from the image can have."
}

# For builds in mainland China, where Docker's own download site and Docker Hub
# are slow or unreachable.
variable "docker_install_mirror" {
  type        = string
  default     = ""
  description = "get.docker.com mirror: Aliyun or AzureChinaCloud."
}

variable "docker_registry_mirrors" {
  type        = string
  default     = ""
  description = "Comma-separated Docker Hub mirrors, kept in the image's daemon.json."
}

# ── per cloud ────────────────────────────────────────────────────────────────

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "aws_instance_type" {
  type    = string
  default = "t3.large"
}

variable "do_region" {
  type    = string
  default = "nyc3"
}

# DigitalOcean: a Droplet made from the snapshot needs at least this size's
# disk, so build on the smallest size whose disk holds the image.
variable "do_size" {
  type    = string
  default = "s-2vcpu-4gb"
}

variable "alicloud_region" {
  type    = string
  default = "cn-hangzhou"
}

variable "alicloud_instance_type" {
  type    = string
  default = "ecs.g7.large"
}

variable "tencentcloud_region" {
  type    = string
  default = "ap-guangzhou"
}

variable "tencentcloud_zone" {
  type    = string
  default = "ap-guangzhou-6"
}

variable "tencentcloud_instance_type" {
  type    = string
  default = "S5.LARGE8"
}

variable "huaweicloud_region" {
  type    = string
  default = "cn-north-4"
}

variable "huaweicloud_flavor" {
  type    = string
  default = "s6.large.4"
}

locals {
  stamp       = formatdate("YYYYMMDDhhmm", timestamp())
  image_name  = "astrabox-${replace(var.astrabox_version, ".", "-")}-${local.stamp}"
  description = "AstraBox ${var.astrabox_version} on Ubuntu 24.04: Docker, the AstraBox images and a first-boot setup with team login and HTTPS."
}

# ── sources ──────────────────────────────────────────────────────────────────

source "amazon-ebs" "astrabox" {
  region          = var.aws_region
  instance_type   = var.aws_instance_type
  ami_name        = local.image_name
  ami_description = local.description
  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    owners      = ["099720109477"] # Canonical
    most_recent = true
  }
  ssh_username = "ubuntu"
  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = var.disk_size_gb
    volume_type           = "gp3"
    delete_on_termination = true
  }
  ena_support = true
  # Machines from the image accept only token-based metadata requests, which
  # astrabox-first-boot makes.
  imds_support = "v2.0"
  tags = {
    Name             = local.image_name
    astrabox-version = var.astrabox_version
  }
}

source "digitalocean" "astrabox" {
  image         = "ubuntu-24-04-x64"
  region        = var.do_region
  size          = var.do_size
  ssh_username  = "root"
  snapshot_name = local.image_name
}

source "alicloud-ecs" "astrabox" {
  region                      = var.alicloud_region
  instance_type               = var.alicloud_instance_type
  image_family                = "acs:ubuntu_24_04_x64"
  image_name                  = local.image_name
  image_description           = local.description
  ssh_username                = "root"
  associate_public_ip_address = true
  internet_charge_type        = "PayByTraffic"
  internet_max_bandwidth_out  = 100
  io_optimized                = true
  system_disk_mapping {
    disk_category = "cloud_essd"
    disk_size     = var.disk_size_gb
  }
}

source "tencentcloud-cvm" "astrabox" {
  region                      = var.tencentcloud_region
  zone                        = var.tencentcloud_zone
  instance_type               = var.tencentcloud_instance_type
  source_image_name           = "Ubuntu Server 24.04 LTS 64bit"
  image_name                  = local.image_name
  image_description           = local.description
  disk_type                   = "CLOUD_PREMIUM"
  disk_size                   = var.disk_size_gb
  associate_public_ip_address = true
  internet_max_bandwidth_out  = 100
  ssh_username                = "ubuntu"
}

source "huaweicloud-ecs" "astrabox" {
  region             = var.huaweicloud_region
  flavor             = var.huaweicloud_flavor
  source_image_name  = "Ubuntu 24.04 server 64bit"
  image_name         = local.image_name
  image_description  = local.description
  volume_type        = "GPSSD"
  volume_size        = var.disk_size_gb
  eip_type           = "5_bgp"
  eip_bandwidth_size = 100
  ssh_username       = "root"
}

# ── build ────────────────────────────────────────────────────────────────────

build {
  sources = [
    "source.amazon-ebs.astrabox",
    "source.digitalocean.astrabox",
    "source.alicloud-ecs.astrabox",
    "source.tencentcloud-cvm.astrabox",
    "source.huaweicloud-ecs.astrabox",
  ]

  # The base image's own first boot (package sources, SSH keys) must finish
  # before apt runs.
  provisioner "shell" {
    inline = ["cloud-init status --wait >/dev/null 2>&1 || true"]
  }

  provisioner "shell" {
    inline = ["rm -rf /tmp/astrabox-image && mkdir -p /tmp/astrabox-image"]
  }

  provisioner "file" {
    source      = "${path.root}/../image/"
    destination = "/tmp/astrabox-image"
  }

  provisioner "file" {
    source      = "${path.root}/../../scripts/install.sh"
    destination = "/tmp/astrabox-image/install.sh"
  }

  provisioner "shell" {
    environment_vars = [
      "ASTRABOX_VERSION=${var.astrabox_version}",
      "ASTRABOX_IMAGE_PREFIX=${var.image_prefix}",
      "ASTRABOX_WARM_ENGINES=${var.warm_engines}",
      "DOCKER_INSTALL_MIRROR=${var.docker_install_mirror}",
      "DOCKER_REGISTRY_MIRRORS=${var.docker_registry_mirrors}",
    ]
    execute_command = "chmod +x {{ .Path }}; sudo -E env {{ .Vars }} bash {{ .Path }}"
    inline          = ["bash /tmp/astrabox-image/build.sh"]
    timeout         = "90m"
  }

  # DigitalOcean's Marketplace checks every submitted image with its own
  # script; run it here so a failure stops the build instead of the review.
  provisioner "shell" {
    only            = ["digitalocean.astrabox"]
    execute_command = "chmod +x {{ .Path }}; sudo -E env {{ .Vars }} bash {{ .Path }}"
    inline = [
      "curl -fsSL https://raw.githubusercontent.com/digitalocean/marketplace-partners/master/scripts/99-img-check.sh -o /tmp/99-img-check.sh",
      "bash /tmp/astrabox-image/cleanup.sh",
      "bash /tmp/99-img-check.sh",
      "rm -f /tmp/99-img-check.sh",
    ]
  }

  provisioner "shell" {
    except          = ["digitalocean.astrabox"]
    execute_command = "chmod +x {{ .Path }}; sudo -E env {{ .Vars }} bash {{ .Path }}"
    inline          = ["bash /tmp/astrabox-image/cleanup.sh"]
  }
}
