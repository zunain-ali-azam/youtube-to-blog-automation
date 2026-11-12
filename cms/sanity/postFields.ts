// Fields yt2blog sets on top of the template's post (title, slug, content, excerpt,
// coverImage, date, author). Spread them into the post type's `fields` array:
//
//   fields: [ ...existingFields, ...yt2blogPostFields ]
import {defineField} from 'sanity'

export const yt2blogPostFields = [
  defineField({
    name: 'seo',
    title: 'SEO',
    type: 'object',
    options: {collapsible: true, collapsed: true},
    fields: [
      defineField({name: 'metaTitle', title: 'Meta title', type: 'string', validation: (rule) => rule.max(60)}),
      defineField({name: 'metaDescription', title: 'Meta description', type: 'text', rows: 3, validation: (rule) => rule.max(160)}),
      defineField({name: 'canonicalUrl', title: 'Canonical URL', type: 'url'}),
    ],
  }),
  defineField({
    name: 'tags',
    title: 'Tags',
    type: 'array',
    of: [{type: 'string'}],
    options: {layout: 'tags'},
  }),
  defineField({
    name: 'video',
    title: 'Source video',
    type: 'object',
    options: {collapsible: true, collapsed: true},
    fields: [
      defineField({name: 'url', title: 'URL', type: 'url'}),
      defineField({name: 'title', title: 'Title', type: 'string'}),
      defineField({name: 'publishedAt', title: 'Published', type: 'datetime'}),
    ],
  }),
  defineField({
    name: 'jsonLd',
    title: 'JSON-LD',
    description: 'VideoObject and FAQPage structured data. The front end puts this in a <script type="application/ld+json"> tag.',
    type: 'text',
    rows: 6,
  }),
]
