import React, { useState, useRef } from 'react';
import {
  Alert, Badge, Button, Form, Stack
} from 'react-bootstrap';
import { Scanner } from '@yudiel/react-qr-scanner';
import AppModal from 'src/components/common/AppModal';
import UIActions from 'src/stores/alt/actions/UIActions';
import ElementActions from 'src/stores/alt/actions/ElementActions';
import UIStore from 'src/stores/alt/stores/UIStore';
import Aviator from 'aviator';
import CodeLogsFetcher from 'src/fetchers/CodeLogsFetcher';
import SearchFetcher from 'src/fetchers/SearchFetcher';

const SCAN_FORMATS = ['qr_code', 'code_128', 'ean_13', 'ean_8', 'data_matrix'];

// Maps a code_log `source` to the model_name/result key expected by the search-by-ids API.
const MODEL_INFO_BY_SOURCE = {
  sample: { modelName: 'sample', resultKey: 'samples' },
  reaction: { modelName: 'reaction', resultKey: 'reactions' },
  wellplate: { modelName: 'wellplate', resultKey: 'wellplates' },
  screen: { modelName: 'screen', resultKey: 'screens' },
  research_plan: { modelName: 'research_plan', resultKey: 'research_plans' },
  device_description: { modelName: 'device_description', resultKey: 'device_descriptions' },
  sequence_based_macromolecule_sample: {
    modelName: 'sequence_based_macromolecule_sample',
    resultKey: 'sequence_based_macromolecule_samples',
  },
  cellline_sample: { modelName: 'cell_lines', resultKey: 'cell_lines' },
  vessel: { modelName: 'vessel', resultKey: 'vessels' },
};

const modelInfoForSource = (source) => (
  MODEL_INFO_BY_SOURCE[source] || { modelName: 'element', resultKey: 'elements' }
);

// Splits a pasted/typed batch of codes on any non-digit separator (space, comma, newline, ...).
const splitManualCodes = (raw) => raw.match(/\d{6,40}/g) || [];

const buildByIdsSelection = (modelName, ids, pageSize) => ({
  elementType: 'by_ids',
  id_params: {
    model_name: modelName,
    ids,
    total_elements: ids.length,
    with_filter: false,
  },
  list_filter_params: {},
  search_by_method: 'search_by_ids',
  page_size: pageSize,
});

const ScanCodeButton = () => {
  const [showModal, setShowModal] = useState(false);
  const [showScanner, setShowScanner] = useState(false);
  const [scanError, setScanError] = useState(null);
  const [multiScan, setMultiScan] = useState(false);
  const [scannedItems, setScannedItems] = useState([]);
  const codeInput = useRef(null);
  const seenKeysRef = useRef(new Set());

  const resetSession = () => {
    setScannedItems([]);
    seenKeysRef.current = new Set();
  };

  const close = () => {
    setShowModal(false);
    setShowScanner(false);
    setScanError(null);
    setMultiScan(false);
    resetSession();
  };

  const navigateToSingleResult = (codeLog) => {
    if (codeLog.source === 'container') {
      // open active analysis
      UIActions.selectTab({ tabKey: 1, type: codeLog.root_code.source });
      UIActions.selectActiveAnalysis({ type: 'sample', analysisIndex: codeLog.source_id });
      Aviator.navigate(`/collection/all/${codeLog.root_code.source}/${codeLog.root_code.source_id}`);
    } else {
      UIActions.selectTab({ tabKey: 0, type: codeLog.root_code.source });
      Aviator.navigate(`/collection/all/${codeLog.source}/${codeLog.source_id}`);
    }
    close();
  };

  // Adds a resolved code_log to the running multi-scan list and fetches its display name.
  const addResolvedCodeLog = (code, codeLog) => {
    // Analysis (container) codes are added as their root element, same as single-scan navigation.
    const target = codeLog.source === 'container' ? codeLog.root_code : codeLog;
    const { modelName, resultKey } = modelInfoForSource(target.source);
    const itemKey = `${modelName}-${target.source_id}`;

    if (seenKeysRef.current.has(itemKey)) return;
    seenKeysRef.current.add(itemKey);

    setScannedItems((items) => [...items, {
      itemKey,
      code,
      source: target.source,
      sourceId: target.source_id,
      modelName,
      resultKey,
      label: `${target.source.replace(/_/g, ' ')} #${target.source_id}`,
      status: 'loading',
    }]);

    const { currentCollection } = UIStore.getState();
    const selection = buildByIdsSelection(modelName, [target.source_id], 1);

    SearchFetcher.fetchBasedOnSearchResultIds({
      selection, collectionId: currentCollection?.id, page: 1, moleculeSort: false,
    }).then((result) => {
      const element = result?.[resultKey]?.elements?.[0];
      const label = (element?.title && element.title()) || element?.name;
      setScannedItems((items) => items.map((item) => (
        item.itemKey === itemKey ? { ...item, status: 'resolved', label: label || item.label } : item
      )));
    }).catch(() => {
      setScannedItems((items) => items.map((item) => (
        item.itemKey === itemKey ? { ...item, status: 'error' } : item
      )));
    });
  };

  const handleScan = (code) => {
    const dataInput = codeInput.current?.value || code;
    if (!dataInput) return;

    CodeLogsFetcher.fetchGenericCodeLogs(dataInput)
      .then((json) => {
        const { code_log: codeLog } = json;
        if (multiScan) {
          addResolvedCodeLog(dataInput, codeLog);
          if (codeInput.current) codeInput.current.value = '';
        } else {
          navigateToSingleResult(codeLog);
        }
      })
      .catch((errorMessage) => {
        setScanError(errorMessage.message);
      });
  };

  const handleManualBatchAdd = () => {
    const raw = codeInput.current?.value || '';
    const codes = splitManualCodes(raw);
    if (codes.length === 0) return;

    CodeLogsFetcher.fetchGenericCodeLogsBatch(codes)
      .then((results) => {
        results.forEach((result) => {
          if (result.code_log) addResolvedCodeLog(result.code, result.code_log);
        });
        const failed = results.filter((result) => result.error);
        setScanError(failed.length > 0 ? `${failed.length} code(s) could not be resolved.` : null);
      })
      .catch((errorMessage) => setScanError(errorMessage.message));

    if (codeInput.current) codeInput.current.value = '';
  };

  const handleScanResult = (results) => {
    if (results?.length > 0) {
      handleScan(results[0].rawValue);
    }
  };

  const handleKeyPress = (e) => {
    const code = e.keyCode || e.which;
    if (code !== 13) return;
    e.preventDefault();
    if (multiScan) {
      handleManualBatchAdd();
    } else {
      handleScan();
    }
  };

  const removeScannedItem = (itemKey) => {
    seenKeysRef.current.delete(itemKey);
    setScannedItems((items) => items.filter((item) => item.itemKey !== itemKey));
  };

  // Groups all scanned elements by type and loads them via the existing search-by-ids pipeline
  // (the same one Advanced Search's "Adopt result" uses), so results render as combined tabs.
  const finalizeMultiScan = () => {
    const { currentCollection, number_of_results: pageSize } = UIStore.getState();
    if (!currentCollection?.id) {
      setScanError('Please select a collection first.');
      return;
    }

    const idsByGroup = {};
    scannedItems.forEach((item) => {
      idsByGroup[item.resultKey] ||= { modelName: item.modelName, ids: [] };
      idsByGroup[item.resultKey].ids.push(item.sourceId);
    });

    const fetches = Object.values(idsByGroup).map(({ modelName, ids }) => SearchFetcher
      .fetchBasedOnSearchResultIds({
        selection: buildByIdsSelection(modelName, ids, pageSize),
        collectionId: currentCollection.id,
        page: 1,
        moleculeSort: false,
      }));

    Promise.all(fetches)
      .then((groupResults) => {
        const combined = Object.assign({}, ...groupResults);
        UIActions.setSearchById(combined);
        ElementActions.dispatchSearchResult(combined);
        close();
      })
      .catch((errorMessage) => {
        setScanError(errorMessage.message || 'Could not load the scanned elements.');
      });
  };

  let primaryActionLabel = 'Start scanning';
  let onPrimaryAction = () => setShowScanner(true);
  let primaryActionDisabled = false;
  if (showScanner && multiScan) {
    const count = scannedItems.length;
    primaryActionLabel = `Done \u2014 show ${count} result${count === 1 ? '' : 's'}`;
    onPrimaryAction = finalizeMultiScan;
    primaryActionDisabled = count === 0;
  } else if (showScanner) {
    primaryActionLabel = undefined;
    onPrimaryAction = undefined;
  }

  return (
    <>
      <Button
        id="search-code-split-button"
        variant="topbar"
        onClick={() => setShowModal(true)}
      >
        <i className="fa fa-barcode" />
        <i className="fa fa-search ms-n2" />
      </Button>

      <AppModal
        show={showModal}
        onHide={close}
        title="Scan barcode or QR code"
        size={multiScan ? 'lg' : undefined}
        primaryActionLabel={primaryActionLabel}
        onPrimaryAction={onPrimaryAction}
        primaryActionDisabled={primaryActionDisabled}
        extendedFooter={(
          <Form.Check
            type="switch"
            id="multi-scan-toggle"
            label="Scan multiple codes"
            checked={multiScan}
            onChange={(e) => setMultiScan(e.target.checked)}
            className="me-auto"
          />
        )}
      >
        <div className="d-flex gap-3">
          <div
            id="code-scanner"
            className="flex-grow-1"
            style={{ maxHeight: '600px', overflow: 'hidden', minWidth: 0 }}
          >
            <Form.Group className="mb-2">
              <Form.Control
                autoFocus
                type="text"
                placeholder={multiScan ? 'Or paste/enter multiple codes...' : 'Or enter code manually...'}
                ref={codeInput}
                onKeyDown={handleKeyPress}
              />
            </Form.Group>

            {showScanner && (
              <Scanner
                onScan={handleScanResult}
                onError={(err) => console.error(err)}
                formats={SCAN_FORMATS}
                styles={{ container: { width: '100%' } }}
              />
            )}
          </div>

          {multiScan && (
            <div
              className="scanned-items-list flex-shrink-0"
              style={{ width: 260, maxHeight: 550, overflowY: 'auto' }}
            >
              <strong>{`Scanned (${scannedItems.length})`}</strong>
              {scannedItems.length === 0 && (
                <div className="text-muted small mt-2">No codes scanned yet.</div>
              )}
              <Stack gap={1} className="mt-2">
                {scannedItems.map((item) => (
                  <div
                    key={item.itemKey}
                    className="d-flex align-items-center justify-content-between border rounded px-2 py-1"
                  >
                    <span className="text-truncate">
                      <Badge bg="secondary" className="me-2">{item.source}</Badge>
                      {item.status === 'loading' ? 'Resolving...' : item.label}
                      {item.status === 'error' && (
                        <span className="text-danger ms-1">(name lookup failed)</span>
                      )}
                    </span>
                    <Button
                      variant="link"
                      size="sm"
                      className="p-0 ms-2"
                      onClick={() => removeScannedItem(item.itemKey)}
                    >
                      <i className="fa fa-times" />
                    </Button>
                  </div>
                ))}
              </Stack>
            </div>
          )}
        </div>

        {scanError && (
          <Alert variant="danger" className="mt-2">{scanError}</Alert>
        )}
      </AppModal>
    </>
  );
};

export default ScanCodeButton;

