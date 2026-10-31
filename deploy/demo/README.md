# A low-cost demo server

The platform on one small AWS server for demos and reviews: the web app behind Caddy (HTTPS from Let's Encrypt), PostgreSQL 16 and the
local calculations worker, all in Docker Compose. It costs about $32 a month left running, or about $7 a month stopped between demos.

This is not the production design. Production is the Terraform stack in `infra/terraform/` (Fargate, RDS, Batch, a separate PII vault),
which the CPUC's cloud team applies. The demo runs with the sandbox's settings: every page carries the sandbox banner, there is no PII vault
(PII uploads are refused), and it must hold only public or synthetic data.

## What it costs (us-west-2, on demand)

| Item | Running all month | Stopped |
| --- | --- | --- |
| t4g.medium (2 vCPU, 4 GB, ARM) | about $25 | $0 |
| 40 GB gp3 disk | about $3 | about $3 |
| Elastic IP (public IPv4) | about $3.60 | about $3.60 |
| **Total** | **about $32** | **about $7** |

Idle, the four containers use about 350 MB of memory. A t4g.large (8 GB, about $50 a month) is the size for a 1,000,000-record ReCET run,
which peaks near 2.3 GB on its own. Use 60 GB of disk if you load the legacy CEDARS years and the calculation reference data (below).

## 1. Launch the server

In the EC2 console:

1. **Launch instance**: Ubuntu Server 24.04 LTS, architecture **64-bit (Arm)**, instance type **t4g.medium**.
2. **Key pair**: create one or choose an existing one. Keep the private key on your own machine only.
3. **Network settings**: a security group that allows SSH (22) from **My IP** only, and HTTP (80) and HTTPS (443) from anywhere. Let's
   Encrypt checks port 80 or 443 from the internet to issue the certificate, so those two must stay open.
4. **Storage**: 40 GiB gp3 (60 GiB for legacy years and calculations).
5. Launch. Then **Elastic IPs**, **Allocate**, and **Associate** it with the instance, so the address (and the site's name) stays the same
   when the server is stopped and started.

## 2. Install Docker

    ssh -i <your key file> ubuntu@<elastic IP>

    curl -fsSL https://get.docker.com | sudo sh        # Docker Engine with the compose and buildx plugins
    sudo usermod -aG docker ubuntu
    curl -LsSf https://astral.sh/uv/install.sh | sh     # uv, to build the engine wheel
    # 2 GB of swap: building the image and loading large files need more than 4 GB at moments
    sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
    exit

Sign in again so the docker group takes effect.

## 3. Get the code and configure it

    git clone https://github.com/JRichardson-PGE/CEDARS-Platform.git
    cd CEDARS-Platform
    scripts/build_engine_wheel.sh                       # the pinned ReCET engine, for Results, Compare, Edit inputs and the worker
    cd deploy/demo
    cp .env.example .env
    nano .env

In `.env`:

- `CEDARS_DOMAIN`: your own host name with a DNS A record pointing at the Elastic IP, or the free name `<IP with dashes>.sslip.io`
  (for an address a.b.c.d that is `a-b-c-d.sslip.io`), which resolves to the address with no DNS setup.
- `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD`: make each with `openssl rand -base64 48 | tr -d '\n/+='`.
- Leave the email settings blank to have emails (sign-up confirmations, password links) printed to the web container's log. To send real
  email, fill in an SMTP server such as Amazon SES.

`.env` holds secrets. It is ignored by git; never commit it.

## 4. Start it

    docker compose up -d --build      # the first build takes about 5-10 minutes
    docker compose ps                 # db, web, worker and caddy up; web "healthy"

The web container applies the database migrations each time it starts. Open `https://<CEDARS_DOMAIN>`; the sandbox banner is at the top.

## 5. First sign-in

    docker compose exec web python manage.py seed_sandbox                      # organisations, a cycle, periods, test deadlines, the dictionary
    docker compose exec web python manage.py bootstrap_cpuc you@example.org --name "Your name" --steward

Then on the site choose **Forgot password**, enter that email, and take the link from the log:

    docker compose logs web | grep password/reset/key

Set a password. The platform then asks you to set up two-factor sign-in with an authenticator app, as it does for everyone (decision F).
Grant other reviewers access from the platform's administration pages; each signs up and confirms their email the same way.

## 6. Optional: data to show

Put files to import in `deploy/demo/import/` on the server (mounted read-only in the containers at `/import`). Copy them from your machine
with `scp -i <your key file> -r <folder> ubuntu@<elastic IP>:CEDARS-Platform/deploy/demo/import/`.

**Evaluation studies** from the public PSR site:

    docker compose exec web python manage.py documents_import_psr --site https://psr.energydataweb.com

**Legacy CEDARS years** from the public record-level downloads (about 5.7 GB, with their `manifest.csv`), in `import/cedars_record_level/`:

    docker compose exec web python manage.py stage_legacy_sources /import/cedars_record_level/manifest.csv /import/cedars_record_level
    docker compose exec web python manage.py load_legacy_filings
    docker compose exec web python manage.py load_legacy_claims

All three can be re-run and skip what is already done. On a 4 GB server load one claim year at a time (`--year 2016`, then the next) if a
run is stopped for memory.

**Calculations** need ReCET's reference data (the ACC parquets and the impact-profile library, about 12 GB) in `import/recet-data/`, laid
out as the engine's reference folder (`acc/<year>/`, `impact_profiles/`). Register each as a draft pack, for example:

    docker compose exec web python manage.py register_pack /import/recet-data/acc/2024 --kind acc_electric --label "2024 ACC" --acc-year 2024
    docker compose exec web python manage.py register_pack /import/recet-data/impact_profiles --kind impact_profiles --label "Impact profiles"

Then add the engine release on the Engine releases page and start a run on New run (README, "Calculations locally"). The `worker` container
prices queued runs. Draft packs are for analysis runs only, which is what a demo needs.

## Stopping, starting and updating

- **Stop** the instance from the EC2 console (or `aws ec2 stop-instances --instance-ids <id>`) when nobody is using it. Starting it again
  brings every container back by itself (`restart: unless-stopped`).
- **Update** to the newest main:

      cd ~/CEDARS-Platform && git pull --ff-only && scripts/build_engine_wheel.sh
      cd deploy/demo && docker compose up -d --build

- **Back up** the database: `docker compose exec -T db pg_dump -U cedars cedars | gzip > cedars-$(date +%F).sql.gz`, or take an EBS
  snapshot of the volume from the console.
- **Remove** the demo: terminate the instance, delete its volume if it was kept, and release the Elastic IP (an unattached one still costs).

## What the demo leaves out

- The daily jobs (`purge_expired`, `send_deadline_reminders`, `close_recertifications`) are not scheduled. Under the sandbox settings
  `purge_expired` deletes filings after 30 days, which would remove demo data; run the other two by hand if you want to show them.
- One server, no load balancer, WAF, Multi-AZ database or automatic backups; a reboot is a few minutes' outage.
- Load-shape jobs run inside the request (`IMPACT_PROFILES_INLINE_JOBS`), instead of in a separate worker.
