#!/bin/bash

IMAGES=(
  "ghcr.io/dustinthal/routerfleet:latest"
  "ghcr.io/dustinthal/routerfleet-monitoring:latest"
  "ghcr.io/dustinthal/routerfleet-nginx:latest"
  "ghcr.io/dustinthal/routerfleet-cron:latest"
)

login_registry() {
  if [ -n "$GHCR_USERNAME" ] && [ -n "$GHCR_TOKEN" ]; then
    echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
  else
    echo "Log in to ghcr.io (this script is the manual alternative to the GitHub Actions workflow):"
    docker login ghcr.io
  fi
}

build_images() {
  cat .gitignore > .dockerignore
  echo "Starting the build of the images..."
  docker compose -f docker-compose-build.yml build
  if [ $? -eq 0 ]; then
    echo "Build completed successfully."
  else
    echo "Error during the image build."
    exit 1
  fi
}

push_images() {
  for IMAGE in "${IMAGES[@]}"; do
    echo "Pushing image: $IMAGE..."
    docker push "$IMAGE"
    if [ $? -eq 0 ]; then
      echo "$IMAGE pushed successfully."
    else
      echo "Error pushing the image: $IMAGE"
      exit 1
    fi
  done
}

login_registry
docker system prune -a
build_images
push_images

echo "Build and push operations completed successfully."