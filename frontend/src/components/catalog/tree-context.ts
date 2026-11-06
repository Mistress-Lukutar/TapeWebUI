// Shared provide/inject contract between the catalog page and tree nodes.

import type { InjectionKey } from 'vue'
import type { CatalogListing } from '../../api'

export interface CatalogTreeContext {
  scope: { kind: 'session' | 'tape'; id: number }
  listingFor(path: string): CatalogListing | undefined
  loadingFor(path: string): boolean
  expandedFor(path: string): boolean
  toggleExpand(path: string): void
  checkedFor(path: string): boolean
  toggleCheck(path: string): void
  activeFor(path: string): boolean
  activate(path: string): void
}

export const CATALOG_TREE_KEY: InjectionKey<CatalogTreeContext> = Symbol('catalog-tree')
