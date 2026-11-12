# Sanity schema for yt2blog

yt2blog writes posts shaped like the `post` type in Sanity's official Next.js blog template
([sanity-template-nextjs-clean](https://github.com/sanity-io/sanity-template-nextjs-clean)).
The template doesn't have everything a generated post contains, so add these to your Studio.

## 1. Code blocks

Install the code input plugin and add it to `sanity.config.ts`:

```bash
npm install @sanity/code-input
```

```ts
import {codeInput} from '@sanity/code-input'

export default defineConfig({
  // ...
  plugins: [/* ... */, codeInput()],
})
```

## 2. The YouTube block

Copy [`youtube.ts`](youtube.ts) into `studio/src/schemaTypes/objects/` and add `youtube` to the
`schemaTypes` list in `studio/src/schemaTypes/index.ts`.

## 3. Allow both in the post body

In `blockContent` (`studio/src/schemaTypes/objects/blockContent.tsx`), add two members to the array's `of`:

```ts
defineArrayMember({type: 'code'}),
defineArrayMember({type: 'youtube'}),
```

## 4. The extra post fields

Copy [`postFields.ts`](postFields.ts) next to `post.ts` and spread it into the post's fields:

```ts
import {yt2blogPostFields} from './postFields'

// in defineType({name: 'post', ...})
fields: [
  // ...the template's fields
  ...yt2blogPostFields,
],
```

## 5. An API token

In [sanity.io/manage](https://www.sanity.io/manage), open the project and go to **API → Tokens → Add API token**. Give it
**Editor** permissions. Put it in `.env` as `SANITY_API_TOKEN`, with the project ID as `SANITY_PROJECT_ID`.

## Rendering on the front end

The template's `PortableText` component doesn't know the two new blocks, so add serializers for them:

```tsx
const components = {
  types: {
    code: ({value}) => <pre><code className={`language-${value.language}`}>{value.code}</code></pre>,
    youtube: ({value}) => {
      const id = new URL(value.url).searchParams.get('v')
      return <iframe src={`https://www.youtube.com/embed/${id}`} allowFullScreen className="aspect-video w-full" />
    },
  },
}
```

Render `post.jsonLd` in the page `<head>` as `<script type="application/ld+json">`, and use `post.seo` for the
metadata.

## Different field names

If your Studio doesn't use the template's names, map them in `.env` instead of renaming anything in the Studio:

```bash
SANITY_POST_TYPE=article
SANITY_FIELDS=content=body,coverImage=mainImage,seo=-   # "-" leaves a field out
```
