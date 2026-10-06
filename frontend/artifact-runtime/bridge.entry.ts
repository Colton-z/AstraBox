import { installBridge } from './bridge';
import type { PageWindow } from './bridge';
installBridge(window as PageWindow);
