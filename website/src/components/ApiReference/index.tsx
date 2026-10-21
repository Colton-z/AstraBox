import React, {lazy, Suspense} from 'react';
import BrowserOnly from '@docusaurus/BrowserOnly';
import useBaseUrl from '@docusaurus/useBaseUrl';
import schema from '@site/../tests/data/openapi_snapshot.json';
import 'swagger-ui-react/swagger-ui.css';
import styles from './styles.module.css';

const SwaggerUI = lazy(() => import('swagger-ui-react'));
const methods = new Set(['get', 'post', 'put', 'patch', 'delete', 'head', 'options']);
const operationCount = Object.values(schema.paths).reduce(
  (count, item) => count + Object.keys(item).filter((key) => methods.has(key)).length,
  0,
);

export default function ApiReference({chinese = false}: {chinese?: boolean}) {
  const options = {
    spec: schema,
    deepLinking: true,
    filter: true,
    docExpansion: 'list' as const,
    defaultModelRendering: 'model' as const,
    defaultModelsExpandDepth: -1,
    supportedSubmitMethods: [],
    plugins: [{components: {OnlineValidatorBadge: () => null}}],
    showExtensions: true,
  };
  return (
    <section className={styles.reference} aria-label="API reference">
      <p data-testid="api-reference-summary">
        {chinese
          ? `版本 ${schema.info.version} · ${Object.keys(schema.paths).length} 个路径 · ${operationCount} 个 HTTP 操作。`
          : `Release ${schema.info.version} · ${Object.keys(schema.paths).length} paths · ${operationCount} HTTP operations.`}
        {' '}<a href={useBaseUrl('/openapi.json')} download="astrabox-openapi.json">
          {chinese ? '下载 OpenAPI JSON' : 'Download OpenAPI JSON'}
        </a>
      </p>
      <BrowserOnly fallback={<p>{chinese ? '正在载入接口目录…' : 'Loading API catalog…'}</p>}>
        {() => <Suspense fallback={<p>Loading API catalog…</p>}><SwaggerUI {...options}/></Suspense>}
      </BrowserOnly>
    </section>
  );
}
