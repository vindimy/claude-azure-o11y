# Built only by CI (or scripts/build-image.sh). BASE_IMAGE comes from build/base-image.txt.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ENV AzureWebJobsScriptRoot=/home/site/wwwroot \
    AzureFunctionsJobHost__Logging__Console__IsEnabled=true \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY host.json /home/site/wwwroot/host.json
COPY src/ /home/site/wwwroot/
COPY config/ /home/site/wwwroot/config/
COPY identity/ /home/site/wwwroot/identity/

WORKDIR /home/site/wwwroot
