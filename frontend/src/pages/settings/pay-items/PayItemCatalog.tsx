import type { BranchConfigVersion, BranchPayDefinitionState, PayDefinitionSummary } from '../../../types/compensation';
import type { AggregateDefinition } from './payItemsModel';
import { fmtDate, INPUT_TYPE_LABELS, METHOD_LABELS } from './payItemsModel';
import styles from './PayItemsPage.module.css';

interface PayItemCatalogProps {
  branchMode: 'single' | 'all';
  loading: boolean;
  loadError: string;
  statusFilter: 'all' | 'active' | 'inactive' | 'mixed';
  setStatusFilter(value: 'all' | 'active' | 'inactive' | 'mixed'): void;
  search: string;
  setSearch(value: string): void;
  visibleItems: BranchPayDefinitionState[];
  selectedId: number | null;
  setSelectedId(value: number | null): void;
  setHistory(value: BranchConfigVersion[] | null): void;
  visibleAggregates: AggregateDefinition[];
  selectedItem: BranchPayDefinitionState | null;
  selectedAgg: AggregateDefinition | null;
  selectedCompany?: PayDefinitionSummary;
  branchName(id: number): string;
  canAllBranches: boolean;
  canGovern: boolean;
  setRetireOpen(value: boolean): void;
  branchEditable: boolean;
  openEdit(): void;
  loadHistory(): Promise<void>;
  history: BranchConfigVersion[] | null;
}

export function PayItemCatalog({
  branchMode, loading, loadError, statusFilter, setStatusFilter, search, setSearch,
  visibleItems, selectedId, setSelectedId, setHistory, visibleAggregates, selectedItem, selectedAgg,
  selectedCompany, branchName, canAllBranches, canGovern, setRetireOpen, branchEditable, openEdit, loadHistory, history,
}: PayItemCatalogProps) {
  return (<div className={styles.body}>
          <div className={styles.listCard}>
            <div className={styles.listToolbar}>
              <div className={styles.statusFilters}>
                {(['all', 'active', 'inactive', ...(branchMode === 'all' ? ['mixed' as const] : [])] as const).map(f => (
                  <button key={f} type="button"
                    className={`${styles.filterPill}${statusFilter === f ? ` ${styles.filterPillActive}` : ''}`}
                    onClick={() => setStatusFilter(f)}>
                    {f.charAt(0).toUpperCase() + f.slice(1)}
                  </button>
                ))}
              </div>
              <div className={styles.searchWrap}>
                <input className={styles.searchInput} type="search" placeholder="Search pay items…"
                  value={search} onChange={e => setSearch(e.target.value)} />
              </div>
            </div>

            <div className={styles.tableWrap}>
              {loadError && <div className={styles.errorAlert} style={{ margin: '1rem' }}>{loadError}</div>}
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>Pay Item</th>
                    <th>Code</th>
                    <th className={styles.tdCenter}>Input</th>
                    <th className={styles.tdCenter}>{branchMode === 'all' ? 'Coverage' : 'Status'}</th>
                  </tr>
                </thead>
                <tbody>
                  {loading && [...Array(4)].map((_, i) => (
                    <tr key={i} className={styles.skeletonRow}>
                      {[...Array(4)].map((__, j) => <td key={j}><div className={styles.skeleton} /></td>)}
                    </tr>
                  ))}
                  {!loading && branchMode === 'single' && visibleItems.map(item => (
                    <tr key={item.pay_definition_id}
                      className={`${styles.tableRow}${selectedId === item.pay_definition_id ? ` ${styles.tableRowSelected}` : ''}${!item.is_active ? ` ${styles.tableRowInactive}` : ''}`}
                      onClick={() => { setSelectedId(item.pay_definition_id); setHistory(null); }}>
                      <td><span className={styles.itemName}>{item.definition_name}</span></td>
                      <td className={styles.rateCell}>{item.definition_code}</td>
                      <td className={styles.tdCenter}>{INPUT_TYPE_LABELS[item.input_type]}</td>
                      <td className={styles.tdCenter}>
                        <span className={item.is_active ? styles.badgeActive : styles.badgeInactive}>
                          {item.is_active ? 'Active' : item.is_configured ? 'Inactive' : 'Not configured'}
                        </span>
                      </td>
                    </tr>
                  ))}
                  {!loading && branchMode === 'all' && visibleAggregates.map(agg => (
                    <tr key={agg.pay_definition_id}
                      className={`${styles.tableRow}${selectedId === agg.pay_definition_id ? ` ${styles.tableRowSelected}` : ''}`}
                      onClick={() => setSelectedId(agg.pay_definition_id)}>
                      <td><span className={styles.itemName}>{agg.definition_name}</span></td>
                      <td className={styles.rateCell}>{agg.definition_code}</td>
                      <td className={styles.tdCenter}>{INPUT_TYPE_LABELS[agg.input_type]}</td>
                      <td className={styles.tdCenter}>
                        <span className={agg.coverage === 'all-active' ? styles.badgeActive
                          : agg.coverage === 'mixed' ? styles.badgeMixed : styles.badgeInactive}>
                          {agg.activeBranches}/{agg.totalBranches} branches
                        </span>
                      </td>
                    </tr>
                  ))}
                  {!loading && !loadError && (branchMode === 'single' ? visibleItems : visibleAggregates).length === 0 && (
                    <tr className={styles.emptyRow}>
                      <td colSpan={4}>
                        {search ? `No pay items match "${search}".` : 'No pay items yet. A company can have none.'}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className={styles.detailPanel}>
            {!selectedItem && !selectedAgg ? (
              <div className={styles.detailEmpty}>
                <p className={styles.detailEmptyTitle}>Choose a pay item</p>
                <p className={styles.detailEmptyText}>
                  Select a pay item to view its branch configuration, effective dates and history.
                </p>
              </div>
            ) : (
              <>
                <div className={styles.detailHeader}>
                  <div>
                    <div className={styles.detailItemName}>{(selectedItem ?? selectedAgg)!.definition_name}</div>
                    <div className={styles.detailCode}>{(selectedItem ?? selectedAgg)!.definition_code}</div>
                  </div>
                </div>
                <div className={styles.detailBody}>
                  <div className={styles.detailSection}>
                    <div className={styles.detailRow}>
                      <span className={styles.detailLabel}>Calculation</span>
                      <span className={styles.detailValue}>
                        {METHOD_LABELS[(selectedItem ?? selectedAgg)!.calculation_method] ?? (selectedItem ?? selectedAgg)!.calculation_method}
                      </span>
                    </div>
                    <div className={styles.detailRow}>
                      <span className={styles.detailLabel}>Input</span>
                      <span className={styles.detailValue}>{INPUT_TYPE_LABELS[(selectedItem ?? selectedAgg)!.input_type]}</span>
                    </div>
                    <div className={styles.detailRow}>
                      <span className={styles.detailLabel}>Unit</span>
                      <span className={styles.detailValue}>{(selectedItem ?? selectedAgg)!.unit ?? '—'}</span>
                    </div>
                    {selectedItem && (
                      <>
                        <div className={styles.detailRow}>
                          <span className={styles.detailLabel}>This branch</span>
                          <span className={styles.detailValue}>
                            {selectedItem.is_active ? 'Active' : selectedItem.is_configured ? 'Inactive' : 'Not configured'}
                          </span>
                        </div>
                        {selectedItem.current_config && (
                          <div className={styles.detailRow}>
                            <span className={styles.detailLabel}>Effective since</span>
                            <span className={styles.detailValue}>{selectedItem.current_config.effective_from}</span>
                          </div>
                        )}
                        {selectedItem.pending_config && (
                          <div className={styles.configBannerPending}>
                            Scheduled: {selectedItem.pending_config.is_active ? 'active' : 'inactive'} from {selectedItem.pending_config.effective_from}
                          </div>
                        )}
                        {selectedItem.notes && (
                          <div className={styles.detailRow}>
                            <span className={styles.detailLabel}>Notes</span>
                            <span className={styles.detailValue}>{selectedItem.notes}</span>
                          </div>
                        )}
                      </>
                    )}
                    {selectedAgg && (
                      <div className={styles.detailRow}>
                        <span className={styles.detailLabel}>Active in</span>
                        <span className={styles.detailValue}>{selectedAgg.activeBranches} of {selectedAgg.totalBranches} branches</span>
                      </div>
                    )}
                    {selectedCompany?.provenance && (
                      <div className={styles.detailRow}>
                        <span className={styles.detailLabel}>Created</span>
                        <span className={styles.detailValue}>
                          {selectedCompany.provenance.creation_mode === 'Request' ? 'From a branch request' : 'Directly by the company'}
                          {' · '}{fmtDate(selectedCompany.provenance.created_at_utc)}
                          {selectedCompany.provenance.requesting_branch_id !== null
                            && ` · ${branchName(selectedCompany.provenance.requesting_branch_id)}`}
                        </span>
                      </div>
                    )}
                  </div>

                  <div className={styles.detailActions}>
                    {(selectedAgg ? canAllBranches : branchEditable) && (
                      <button type="button" className={styles.btnPrimary} onClick={openEdit}>Edit Configuration</button>
                    )}
                    {selectedItem && (
                      <button type="button" className={styles.btnSecondary} onClick={() => void loadHistory()}>History</button>
                    )}
                    {selectedCompany && selectedCompany.status !== 'Retired' && canGovern && (
                      <button type="button" className={styles.btnDanger} onClick={() => setRetireOpen(true)}>Retire</button>
                    )}
                  </div>

                  {history && (
                    <div className={styles.historySection}>
                      <div className={styles.detailSectionTitle}>Configuration history</div>
                      {history.length === 0 && <div className={styles.cdpiEmpty}>No configuration yet.</div>}
                      {history.map(version => (
                        <div key={version.config_id} className={styles.historyItem}>
                          <span>{version.is_active ? 'Active' : 'Inactive'}</span>
                          <span className={styles.historyRange}>
                            {version.effective_from} – {version.effective_to ?? 'present'}
                          </span>
                          {version.notes && <span className={styles.historyMeta}>{version.notes}</span>}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
  );
}