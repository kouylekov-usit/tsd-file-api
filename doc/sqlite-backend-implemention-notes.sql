
/*

To understand the SQL generation code for the URI based JSON query
language, implemented by parser.py, work through these examples, and
keep https://www.sqlite.org/json1.html open for a reference.

*/

-- all tables handled by the SQL, generated by parser.py
-- have the following definition

create table if not exists mytable (data json unique not null);

-- let's add some test data
insert into mytable values (json(
    '{
        "x": 0,
        "y": 1,
        "z": null,
        "b":[1, 2, 5, 1],
        "c": null,
        "d": "string1"
    }'
));
insert into mytable values (json(
    '
    {
        "y": 11,
        "z": 1,
        "c": [
            {
                "h": 3,
                "p": 99,
                "w": false
            },
            {
                "h": 32,
                "p": false,
                "w": true,
                "i": {
                    "t": [1,2,3]
                }
            },
            {
                "h": 0
            }
        ],
        "d": "string2"
    }'
));
insert into mytable values (json(
    '{
        "a": {
            "k1": {
                "r1": [1, 2],
                "r2": 2
            },
            "k2": ["val", 9]
        },
        "z": 0,
        "x": 88,
        "d": "string3"
    }'
));
insert into mytable values (json(
    '{
        "a": {
            "k1": {
                "r1": [33, 200],
                "r2": 90
            },
            "k2": ["val222", 90],
            "k3": [{"h": 0}]
        },
        "z": 10,
        "x": 107
    }'
));
insert into mytable values (json(
    '{
        "x": 10
    }'
));

/*

The goal of the parser.py is to implement a URI based JSON query
language that exposes some of the features of SQL, allowing
users of the API to select subsets of their JSON data
while at the same time giving the possibility to
filter, reorder, and paginate it.

To make this more concrete, the target data typically looks like:

[
    {k1: v, k2: [v]},
    {k1: v, k2: [v]},
    {k1: v, k3: [{k:v}, {k:v}]},
]

Each map in the array is a row in the table. The query language
supports selecting specific keys from the maps in the array,
generating results like:

[
    {k1: v},
    {k1: v},
    {k1: v},
]

Filters can be applied to maps in the array / rows in the table,
to prooduce results like

[
    {k1: v, k3: [{k:v}, {k:v}]},
]

Reordering and pagination can further be applied to change
the rows returned.

In addition to selection functionality, it is possible to update values
and to delete entries, using the same selection and filtering mechanisms.
The query language has some limitations, due to  two important constraints
when implementing the SQL code generation: 1) the limits of the sqlite json1
extension, and 2) the ability to keep the code generation code maintainable.

*/

-- now let's do some basic JSON selections to see what kind of SQL code
-- is generated

select json_extract(data, '$.x') from mytable;
-- this produces a simplified result, throwing away the JSON structure
-- the HTTP/JSON query language will preserve the shape, so this
-- has to be implemented by rebuilding the original JSON

select json_object('x', json_extract(data, '$.x')) from mytable;
-- and this is the SQL generation target:
-- json_object(key1, json_extract(data, '$.key1'), key2, json_extract(data, '$.key2'))
-- the above strategy is good enough for key selection which does not involve arrays
-- for selecting specific entries in an array like key2[0] we can rely on array
-- functionality in the json1 extension, e.g.

select json_object(data, '$.b[0]') from mytable;
-- this once again return the simplified result, discarding json structure
-- to reconstruct the original we do the following

select json_object(
    'b',
    case when json_extract(data, '$.b[0]') is not null
    then json_array(json_extract(data, '$.b[0]'))
    else null end) from mytable;
-- the reason for wrapping the call to json_extract(data, '$.b[0]') in a case statement
-- is that it will otherwise return [null] instead of null
-- this is the second SQL generation target

-- when array elements are maps, instead of single elements like strings, or scalars
-- then one typically wants to perform key selection inside the array elements
-- and then typically, one wants those selections to apply to all elements
-- just like it does on the uppper level
-- in this case, the json1 extension does not provide high-level suport, so
-- we need to use lower level features to accomplish the selection
-- and JSON reconstruction

-- the function we use to handle key selection inside array elements
-- is json_tree: https://www.sqlite.org/json1.html#jtree - it walks
-- the JSON, returning one row for each element.

select key, value, fullkey, path from mytable, json_tree(mytable.data);

-- The generic approach to key selection inside array elements, with optional
-- broadcasting over the array and thereby the third SQL generation
-- strategy. Shown below:

select json_object(
    -- key
    'c',
    -- slice and select in array elements
    (case when json_extract(data, '$.c') is not null then (
        select json_group_array(vals) from (
            select json_object(
                -- specify keys to select
                'h', json_extract(value, '$.h'),
                'p', json_extract(value, '$.p')) as vals
            from (
                select key, value, fullkey, path
                from mytable, json_tree(mytable.data)
                where path = '$.c'
                -- add optional index value "n"
                -- and fullkey = '$.c[n]'
                )
            )
        )
    else null end)
) from mytable;

-- TODO...
-- POC: setting inside slices

-- set=b[2].66
-- todo
select json_object('b', json_group_array(vals)) from (
    -- when key = idx
    select case when key = 2 then 66 else key end as vals from(
        select key, value, fullkey, path
        from mytable, json_tree(mytable.data)
        where path = '$.b'
    )
);

-- set=a.k1.r1[0].35
-- todo

-- set=c[0].h.4
-- todo

