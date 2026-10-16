import React, { useState, useRef, useContext } from 'react';
import { Alert, Button, Form } from 'react-bootstrap';
import { Scanner } from '@yudiel/react-qr-scanner';
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels';
import AppModal from 'src/components/common/AppModal';
import UIActions from 'src/stores/alt/actions/UIActions';
import Aviator from 'aviator';
import CodeLogsFetcher from 'src/fetchers/CodeLogsFetcher';
import SearchFetcher from 'src/fetchers/SearchFetcher';
import { StoreContext } from 'src/stores/mobx/RootStore';
import { ALL_LABEL } from 'src/stores/mobx/CollectionsStore';
import { byIdsModelParams } from 'src/utilities/searchRequestParams';

const SCAN_FORMATS = ['qr_code', 'code_128', 'ean_13', 'ean_8', 'data_matrix'];

const ScanCodeButton = () => {
  const collectionsStore = useContext(StoreContext).collections;
  const [showModal, setShowModal] = useState(false);
  const [showScanner, setShowScanner] = useState(false);
  const [scanError, setScanError] = useState(null);
  const [multiScan, setMultiScan] = useState(false);
  const [scanResults, setScanResults] = useState({ items: [], info: null });
  const { items: scannedItems, info: scanInfo } = scanResults;
  const codeInput = useRef(null);

  const close = () => {
    setShowModal(false);
    setShowScanner(false);
    setScanError(null);
    setMultiScan(false);
    setScanResults({ items: [], info: null });
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

  const addResolvedCodeLog = async (code, codeLog) => {
    if (codeLog?.source === 'container') {
      throw new Error('Multi scan does not support analysis container codes.');
    }

    const { model_name: modelName } = byIdsModelParams(codeLog.source);
    if (modelName === 'element' && codeLog.source !== 'element') {
      throw new Error(`Unsupported ELN entity type: ${codeLog.source}.`);
    }

    const allCollection = collectionsStore.systemCollections.find(
      (collection) => collection.is_locked && collection.label === ALL_LABEL
    );
    if (allCollection?.id == null) {
      throw new Error('Your All collection is not available yet.');
    }
    const result = await SearchFetcher.fetchBasedOnSearchResultIds({
      selection: {
        elementType: 'by_ids',
        id_params: {
          model_name: modelName,
          ids: [codeLog.source_id],
          total_elements: 1,
          with_filter: false,
        },
        list_filter_params: {},
        search_by_method: 'search_by_ids',
        page_size: 1,
      },
      collectionId: allCollection.id,
      page: 1,
      moleculeSort: false,
    });
    const entity = Object.values(result || {})
      .flatMap((group) => (Array.isArray(group?.elements) ? group.elements : []))
      .find((item) => String(item.id) === String(codeLog.source_id));
    if (!entity) {
      const sourceLabel = codeLog.source.replace(/_/g, ' ');
      throw new Error(`Could not load ${sourceLabel} #${codeLog.source_id} in your All collection.`);
    }
    setScanResults(({ items }) => {
      const alreadyScanned = items.some(
        (item) => item.source === codeLog.source && String(item.entity.id) === String(entity.id)
      );
      return alreadyScanned
        ? { items, info: 'This item has already been scanned.' }
        : { items: [...items, { code, source: codeLog.source, entity }], info: null };
    });
    return entity;
  };

  const handleScan = (code) => {
    if (!code) return;
    CodeLogsFetcher.fetchGenericCodeLogs(code)
      .then(async (json) => {
        const { code_log: codeLog } = json;
        if (multiScan) {
          await addResolvedCodeLog(code, codeLog);
          setScanError(null);
          return;
        }
        navigateToSingleResult(codeLog);
      })
      .catch((errorMessage) => {
        setScanError(errorMessage.message);
        setScanResults((current) => ({ ...current, info: null }));
      });
  };

  const handleScanResult = (results) => {
    if (results?.length > 0) {
      handleScan(results[0].rawValue);
    }
  };

  const handleKeyPress = (e) => {
    const code = e.keyCode || e.which;
    if (code === 13) {
      e.preventDefault();
      handleScan(codeInput.current?.value);
    }
  };

  const isMultiScanRunning = multiScan && showScanner;

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
        style={{ '--bs-modal-width': multiScan ? '1000px' : '500px' }}
        dialogClassName="scan-code-modal-dialog"
        primaryActionLabel={isMultiScanRunning ? 'Done' : 'Start scanning'}
        onPrimaryAction={isMultiScanRunning ? close : () => setShowScanner(true)}
        primaryActionDisabled={isMultiScanRunning && scannedItems.length === 0}
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
        <PanelGroup
          key={multiScan ? 'multi-scan' : 'single-scan'}
          direction="horizontal"
          style={{ height: 'auto' }}
        >
          <Panel
            id="scan-code-scanner-panel"
            order={1}
            defaultSize={multiScan ? 50 : 100}
            minSize={20}
          >
            <div
              id="code-scanner"
              className="flex-grow-1"
              style={{ minWidth: 0 }}
            >
              <Form.Group className="mb-2">
                <Form.Control
                  autoFocus
                  type="text"
                  placeholder="Or enter code manually..."
                  ref={codeInput}
                  onKeyDown={handleKeyPress}
                />
              </Form.Group>

              {showScanner && (
                <Scanner
                  onScan={handleScanResult}
                  onError={(err) => console.error(err)}
                  formats={SCAN_FORMATS}
                  styles={{
                    container: {
                      width: '100%',
                      maxWidth: 'min(600px, calc(70vh - 6rem))',
                      height: 'auto',
                      aspectRatio: '1 / 1',
                      marginInline: 'auto',
                    },
                    video: {
                      position: 'absolute',
                      inset: 0,
                      objectPosition: 'center',
                    },
                  }}
                />
              )}
            </div>
          </Panel>

          {multiScan && (
            <>
              <PanelResizeHandle
                className="d-flex align-items-center justify-content-center bg-light"
                aria-label="Resize scanner and scanned items"
                style={{ width: 16, minHeight: 84, cursor: 'col-resize' }}
              >
                <span
                  aria-hidden="true"
                  className="bg-secondary rounded"
                  style={{ width: 4, height: 32 }}
                />
              </PanelResizeHandle>
              <Panel
                id="scan-code-list-panel"
                order={2}
                defaultSize={50}
                minSize={20}
              >
                <div
                  className="scanned-items-list"
                  style={{ maxHeight: 550, overflowY: 'auto' }}
                >
                  <strong>{`Scanned (${scannedItems.length})`}</strong>
                  {scannedItems.length === 0 && (
                    <div className="text-muted small mt-2">No codes scanned yet.</div>
                  )}
                  <ul className="list-unstyled mt-2 mb-0">
                    {scannedItems.map(({ source, entity }, index) => (
                      // Entries are append-only until the list is cleared.
                      // eslint-disable-next-line react/no-array-index-key
                      <li key={index} className="border-bottom py-2">
                        {`${source.replace(/_/g, ' ')} #${entity.id}`}
                      </li>
                    ))}
                  </ul>
                </div>
              </Panel>
            </>
          )}
        </PanelGroup>

        {multiScan && scanInfo && (
          <Alert variant="info" className="mt-2">{scanInfo}</Alert>
        )}
        {scanError && (
          <Alert variant="danger" className="mt-2">{scanError}</Alert>
        )}
      </AppModal>
    </>
  );
};

export default ScanCodeButton;