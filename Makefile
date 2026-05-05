# Makefile — convenience wrappers around the 7-step automation
#
# Required env: UPSTAGE_API_KEY
# Optional env: NOTION_TOKEN, NOTION_LIBRARY_DB_ID

CUSTOMER_DIR ?= ../../inputs
SCHEMA       ?= examples/trade_invoices.json
OUT_DIR      ?= results
SET_DIR      ?= ../trade_invoices_set
AGENT_URL    ?= https://studio.upstage.ai/agents/agt_8dSHuEBXsm9mmNxszqWn9Y
WORKERS      ?= 4

.PHONY: install setup pipeline batch synth synth-batch sync seo all clean

install:
	pip install -r requirements.txt --break-system-packages

setup:
	@test -n "$$UPSTAGE_API_KEY" || (echo "Set UPSTAGE_API_KEY (see .env.example)" && exit 1)

# Step 2 — single file end-to-end
pipeline: setup
	python pipeline.py "$(FILE)" --schema $(SCHEMA)

# Step 2 — folder batch
batch: setup
	python batch.py "$(CUSTOMER_DIR)" --schema $(SCHEMA) \
		--out-dir $(OUT_DIR) --workers $(WORKERS)

# Step 4 — single file synth (HTML)
synth: setup
	python synth.py "$(FILE)" --include-parse

# Step 4 — folder synth + verification
synth-batch: setup
	python synth_batch.py "$(CUSTOMER_DIR)" \
		--out-dir synth_results --workers 3 --render --verify \
		--schema $(SCHEMA)

# Step 5-6 — push set to Notion
sync: setup
	python notion_sync.py --set $(SET_DIR) --agent-url "$(AGENT_URL)"

# Step 7 — generate SEO meta tags
seo:
	python seo_meta.py --card $(SET_DIR)/library_card.json

# Run everything in order — useful for the first time on a new customer
all: setup
	$(MAKE) batch
	$(MAKE) synth-batch
	$(MAKE) sync
	$(MAKE) seo

clean:
	rm -rf $(OUT_DIR) synth_results
