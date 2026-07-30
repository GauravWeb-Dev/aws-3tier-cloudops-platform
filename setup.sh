
#!/usr/bin/env bash
# Phase 0: Scaffolding script for cloned GitHub repository

set -euo pipefail

echo "🚀 Phase 0 Scaffolding Starting..."

# 1. Create directory structure
mkdir -p infra/permanent/reaper \
         infra/demo/network \
         infra/demo/compute \
         infra/demo/data \
         infra/demo/edge \
         services/frontend \
         services/api \
         services/redis \
         iam \
         .github/workflows \
         docs \
         scripts

# 2. Create .gitignore file
cat > .gitignore <<'EOF'
.terraform/
*.tfstate
*.tfstate.backup
.terraform.lock.hcl
node_modules/
.env
*.zip
build/
EOF

# 3. Create placeholder README if not exists
if [ ! -f README.md ]; then
  echo "# aws-3tier-cloudops-platform" > README.md
fi

# 4. Commit structure to main branch
git add .
git commit -m "chore: Phase 0 repository scaffolding" || echo "Nothing to commit"

# 5. Create and push develop branch
git branch -M main
git checkout -b develop
git push -u origin develop || echo "Failed to push develop branch. Check remote configuration."

# 6. Switch back to main
git checkout main
git push -u origin main || echo "Failed to push main branch."

echo "✅ Phase 0 Scaffolding Complete!"
